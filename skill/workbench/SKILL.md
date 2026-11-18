---
name: workbench
description: >
  Workbench mode for the rest of the session: Claude hands the coding of each request to local
  models running in OpenCode, via the workbench `wb` CLI, which routes each task to the best
  local model for its size; Claude writes the spec, reviews the diff, and accepts or sends
  feedback, so Claude spends tokens on specs and review instead of code generation. Only when
  the user invokes /workbench — never on your own.
disable-model-invocation: true
argument-hint: "[first task] [--model <id>] [--merge]"
---

# /workbench — local agent writes, Claude reviews

The user's Claude budget is tight; that is the point of this mode. You are **architect and
reviewer**: write a precise spec, hand it to a local agent with `wb`, let the runner's test gate
and auto-fix rounds do the cheap iterations, review only what passes, then accept or steer. Don't
write the code yourself unless the escape hatch fires.

**You run the show, and the bar is yours.** Delegating saves tokens on typing, never on quality.
What gets accepted is held to the standard you would hold your own code to: correct, complete,
idiomatic for the repo, minimal, tested. Never think "good enough for a local model". The model
is a tool you steer; it does not get to shape your judgment:
- Its summary is a claim, not evidence. "Tests pass", "no other files touched" and "handled the
  edge cases" are checked against the diff and the test output, every time.
- Its framing doesn't anchor you. Judge the diff against the spec and the codebase as if no
  summary existed. A confident or polished summary earns nothing.
- Don't drift. If round 3 still falls short, fix it yourself or discard it. Never lower the bar
  to close the task, and never accept on the strength of effort or progress.
- When the agent disagrees with the spec, weigh its reason on the merits. Change your mind only
  if it is right, and say so in the feedback.

**Roles.** You and the agent are a team, and you are its lead. The agent writes the code; you
own the architecture, CLAUDE.md, AGENTS.md and `.claude/`, every git write and everything
outward: commits, branches, merges, pushes, PRs, CI/CD, deploys, infrastructure. It knows this
from its handbook and will ask (`ask_reviewer`) or say so in its summary when the task needs one
of those; then you do it. Its hard limits: no sudo, git read-only (a `git` of workbench's own on
its PATH; scratch repos under the temp directory, which tests make, stay writable), no `gh`, and
no edits to CLAUDE.md, AGENTS.md or `.claude/`. The shell rules refuse the direct forms; behind
them, where `wb doctor` reports Landlock, the kernel refuses root and writes to the repo's git,
and no credential is in the agent's reach, so `gh` or a push gets no login. Without Landlock
(macOS, old kernels) only the rules hold: read the report's flagged files closely and check
`git -C <repo> log` after a task. The rules stop mistakes, not a determined agent: the
round report flags any of your files it changed anyway (through the shell). Talk to it like a teammate: answer its questions, send notes while
it works, explain the why in feedback.

**Commits are the user's.** Delegated work is committed under the user's own git identity with
no `Co-Authored-By` or other AI credit line; `wb accept` writes only the task's provenance
trailers. Don't add one yourself.

## Workbench mode
`/workbench` turns workbench mode on for the rest of this session; the Workbench pane opening is
the user's sign that it is on. The user then works with you as usual, in plain words: each
request is handled the way this skill says, until they run `/workbench-quit` or
`/workbench-quit-and-hand-back-claude`. Running `/workbench` again while it is on only brings
the pane back (the mod answers it; with a task, the task is yours to take as usual).
- **First task:** `$ARGUMENTS`. Empty? Reply in one line that workbench mode is on and wait for
  their first request. Don't scan the repo or check models until there is work.
- **What goes to the agents:** the coding: features, fixes, refactors, tests, boilerplate.
  Answer questions, read code, plan, and do what is yours (git, CLAUDE.md, `.claude/`, anything
  outward) yourself, as always. An edit that costs less to make than to spec (a typo, one line)
  you may just make.
- **After a compaction or `/clear`**, a `workbench-mode` context block says the mode is still on
  and where this file is: read it again if its procedure is no longer in your context.
- **Plans are handoff plans**, in plan mode or not: see "Planning work you will hand off" below.

`wb` is the workbench CLI (on PATH as `wb`; `wb --help`). Its code, ARCHITECTURE.md included, is
two directories above `readlink -f "$(command -v wb)"`. Every command
prints a short report written for you — don't go digging in raw logs unless a report says to.

## 0. The project: git and CLAUDE.md are yours
Every project you delegate in is a git repo with a CLAUDE.md that tells the truth, and you keep
it that way; the agents never write either.
- **New project**: `wb init <dir>` makes the repo (git's default branch, an initial commit); `<dir>`
  is the project's own directory, never ~. When it refuses likely secrets or installed directories,
  write a `.gitignore` for them (ask the user about a secret it names) and run it again. Never add a
  remote or create a GitHub repo until the user gives the name and says private or public; you
  may ask once the project has a shape.
- **CLAUDE.md**: before the first task, write it from what you know and what you read (what the
  project is and is not, how to build, run and test it, the layout, conventions, decisions made
  and things deliberately not done), commit it, and run `wb init <dir>` again: AGENTS.md becomes
  a symlink to it, so OpenCode's agents read the same file. Keep it short and factual; it goes
  into every agent prompt. An existing AGENTS.md is moved to CLAUDE.md by `wb init`.
- **Keeping it current**: `wb accept` prints how many code commits CLAUDE.md is behind. After
  each accepted task, before the next spec, update it for whatever changed (a new module, command,
  dependency, convention or decision) and commit it on its own (`docs: CLAUDE.md …`). Nothing
  changed that it describes? Leave it. It says what the project is, never what comes next: keep
  the plan and the next step in your own notes. An agent that loses its task takes up whatever
  CLAUDE.md says is next (seen: a compaction summary that recounted CLAUDE.md, and the agent
  built its "Next:" line instead of its spec). `wb start` refuses a repo without one (CLAUDE.md or
  `.claude/CLAUDE.md`; `--no-claude-md` only for throwaway repos). It also refuses an unknown
  `--base` and a repo with no git identity for the commits: fix those, don't work around them.
- **Submodules** are not checked out in the agent's worktree (`wb start` says so): a step whose
  build or tests need one is yours, or its acceptance command must not need it.

## Planning work you will hand off
In workbench mode every plan you write is for agents weaker than you, who see only the spec you
send, so the plan the user approves is already the list of specs, each sized and checked by §1
and §2. That holds in plan mode, which is read-only: read the code, `wb models` and `wb status`
if you can, and start nothing until the plan is approved; then write the first spec and
`wb start` at once. Per step:
- **One concern**, no bigger than the slice `wb models` gives the model that will take it, and
  its size (trivial or significant). A step you can't size is two steps. Size it by the bytes the
  agent will read (`wc -c`), not the files: a two-line edit in a 2,000-line file still gets the
  file read (seen: 39% of a 32K window gone before the first edit, then a compaction).
- **The files**: those to read first, those to create or change, and the existing code to imitate.
- **Exact interfaces** for anything later steps or other code rely on: names, signatures,
  constants, file formats. Settle them in the plan; agents rename what isn't pinned.
- **Its acceptance command**, and the worked examples it tests, each number checked against
  every rule. A step whose done-ness you can't state as a command isn't ready to hand off.
  The runner runs it once on the base, in the agent's lane, before round 1: what it saw comes in
  the first report, and a check-in at once when it already passes. Read it before you judge the
  agent's work. A test that writes outside the lane or races under load
  fails there whatever the agent does, and the agent spends its rounds on failures it didn't
  cause (seen: `whoami` and /tmp probes); a command that already passes can't tell when the
  step is done. Either way, `wb discard` and start again with a better one. It should fail only for
  what the step changes, and check every listed change (each old string gone, each new one
  there), not just the theme: a small model skips one item of a list.
- **Order**: logic, then its tests, then UI; a step that needs another's interface comes after it,
  through `wb next` on the same agent when it is the same area.
- **What stays yours**, marked apart: git, CLAUDE.md, choices and setup only the user can make
  (downloads, licences, public repos), and the checks you run at review (a screenshot to look at,
  tests to break on purpose, a grader).
Keep code out of the plan except those interfaces: the agents write it, you hold the bar. The plan
lives in your notes or the scratchpad, never in CLAUDE.md: an agent that loses its task takes up
whatever CLAUDE.md says is next.

## 1. Size it; the router picks the model
| Size | Typical task |
|---|---|
| `--size significant` | new feature, multi-file change, refactor, non-obvious bug, design judgment, >~50 lines |
| `--size trivial` | rename, string/config tweak, a test for existing code, small single-function change, boilerplate |

`wb start` asks the router, which makes an educated guess and corrects it with real results:
the largest model allowed significant work, the smallest allowed trivial work, unless a model's
record of accepted tasks at that size is clearly worse. It prints why; the runner then loads that
model, unloading whatever is there. `wb models` shows the ranking per size. Pass
`--model <id or fragment>` only if the user names one, or when the record argues against the guess.
Which sizes a model takes is set with `wb model sizes <id> trivial|significant`: set it when you
add a model you judge fit only for small work, and otherwise change it only when the user asks.
A newly added model has no record, so the router won't pick it over one with a good record:
give it its first few real tasks with `--model` (ones you'd review closely anyway).
**No local model yet?** If `wb models` lists none that can run, do the model check below first:
recommend models for this hardware and offer to set them up in OpenCode (ask before any download).
**Work with the model server the user already runs**, whichever it is: `wb scout` lists the server
commands on PATH and the servers answering here. Never steer them to one they don't use. With
none at all, lay out the options that fit the machine in a line each (llama.cpp's `llama-server`,
alone or behind llama-swap; LM Studio; Ollama; vLLM on NVIDIA; MLX on Apple Silicon) and let the
user pick; install nothing unasked. Asked which is most hands-off: Ollama (you download and remove
its models, its tasks can share the GPU), then LM Studio. Whatever you set up, set it up for
agents: a window of 32K or more (16K at the least), tool calls on (vLLM `--enable-auto-tool-choice
--tool-call-parser <the model's>`), and `wb doctor` after. For a server OpenCode has no provider for: `wb model add
<provider>/<model> --url <its baseURL> --context N`, plus `--start`/`--stop` for one that runs one
model a process (llama-server, vLLM). Ollama running with no provider: `wb model add
ollama/<model>` writes it itself. `wb doctor` names each provider it skipped and why; one on
another host that is the user's own server (WSL2's Windows host, a LAN box) needs their yes for
`local: true`. Ollama cloud models (`:cloud`) run off this machine: never register one without
the user's explicit yes.

**Updating workbench.** The mod checks weekly and tells the user; `/workbench-update` is theirs.
Asked to update it yourself: `wb update` (it refuses while tasks run; tell the user to restart
Claude Code or run `/reload-plugins` after).

**The weekly model check.** Once a week the mod asks you (or the user runs `/workbench-models`)
to check the local models against this machine. `wb scout` gives the facts: GPU and VRAM, RAM,
disk, what is installed and registered, real-task results, and the watch list
(models set aside, with what each needs before it is tried again: check each against what is
new). Then research what is
newest and best: open-weight coding models with reliable tool calls, the sizes and quantizations
that fit the VRAM with room for the context, and runtime releases worth having. Use primary
sources (their APIs first; a blocked page is not checked: *The web is yours*, below) and its
reputation in recent reports, verify before you name anything. Fit the models to the machine `wb scout` describes, whatever
its size: a model that fits entirely in VRAM (or the GPU's share of unified memory) beats a bigger
one that spills into RAM, and on a big card the best is a larger model, a longer context, or both.
The window sets the slice of every task (`slice:` in the report), so where the VRAM has room past
the weights, raise the context the server serves (llama-server `-c`, LM Studio's context length,
vLLM `--max-model-len`, Ollama's `OLLAMA_CONTEXT_LENGTH`), then `wb model add … --context N`;
`wb model add` reads the window from the server (Ollama, llama-server, LM Studio once the model
is loaded, vLLM) and refuses when it can't say: then `--context N`, the window it was started
with; ask the user if you don't know it. `wb doctor` flags an entry whose
context differs from what the server serves, or that has none. Results first, then speed. Recommend briefly (keep / add / retire, with sizes and why), and
only with a yes: on Ollama `wb model add <provider/model> --pull` (background); on another server,
download it the server's own way (`lms get`, a GGUF from Hugging Face) and `wb model add`; `wb model sizes` for what it
should take, `wb model retire <id> [--delete]`. Installed but unregistered models may belong to other tools: ask before touching
them. When you set a model aside, `wb scout --watch "…"` records what would bring it back:
the change to look for, and the test it must pass. Finish with `wb scout --done --notes "…"`. Nothing worth changing: one line, and done.

## 2. Spec (the most valuable thing you write)
Read just enough of the repo to scope it, then write the spec to a file in the scratchpad:
- the goal in 1–2 sentences; the exact files/functions to touch, and what not to touch
- conventions to follow (point at an existing file to imitate)
- acceptance criteria, and an **acceptance command** that proves them (an existing test command,
  or a new test the agent must write first — say which)

**Before `wb start`: check the spec.** Spec gaps cause review rounds. You check the numbers you
compute, but they hide in the sentences you didn't execute. Go through these, each from a real
miss:
1. **Every rule has a test.** For each "must", "rejects", "at most" or "only", name the test
   that fails if it's broken. A rule without one is a wish (seen: "one trailing newline"
   was stated, no test rejected a blank row, and the parser overflowed on one).
2. **Every edge input is named:** empty, one, the maximum, one past it, and for text, separators
   that lead, double or trail.
3. **Rules checked against each other and against the algorithm.** A size limit and a method
   can conflict: a heap that skips stale entries holds one entry per push, not per item. Give
   each fixed size its bound and why it holds.
4. **Every number run, not reasoned:** compute each expected value with a script, through the
   limits the spec sets (a clamp, a cap, integer division, float rounding). Seen: 24,900 of
   32,768 printed as 75%.
5. **Every fact about the repo checked in the repo:** each name, field, file and build step you
   mention (`grep` it). Memory is not a source. Seen: a field called `in.x` that was `move_x`,
   and "add it to the Makefile" for a Makefile that finds files by itself.
6. **Only the task.** Nothing about later steps, in the spec or in CLAUDE.md. An agent takes
   any "next" as its job.
7. **The reading fits.** Name only the files the agent must read, headers before sources, and
   put a short fact it needs (a signature, a constant) in the spec instead of a file to read.
   The repo's CLAUDE.md is already in its instructions: asking it to read that is reading twice.
   Spec plus named files (`wc -c`, ~4 characters a token) well under half the window, since the
   agent reads more than you name. Seen: a step whose reading filled 60% of a 32K window
   before the first edit, twenty sessions in a row.
8. **The old tests still hold.** A change of behaviour breaks tests that assumed the old one:
   run the whole suite against your simulated change (or read each test the change touches)
   and say in the spec which tests change and how. Seen: a smarter pursuit reached its target
   sooner, so a test measuring distance from the starting point failed, and a new collision rule
   erased an input a replay test relied on: two of the agent's questions, both the spec's.
9. **Every insertion point is a line to quote.** In a file the agent reads only in part, say
   where new code goes by the exact line it follows, unique in the file, not "after function
   X": it can't see where X ends. Seen: new tests put after a test's first `wait` call, its
   last eleven lines deleted, and the summary claiming otherwise.
After review, when a round was the spec's fault, say so: `--spec-gap "what it missed"` on
`wb feedback`, `next`, `accept` or `discard`. `wb start` shows your record from those,
and a gap that recurs belongs in this list.

`wb` appends the working protocol itself (no commits, run the tests, stay in scope, ask_reviewer
budget, the summary format), so don't repeat it. The agent also starts with what you would have:
the repo's CLAUDE.md / AGENTS.md / CLAUDE.local.md / `.claude/rules`, a map of nested CLAUDE.md
files, the workbench handbook (`agent/HANDBOOK.md`: read first, match the code, smallest diff,
verify, honest summary) and the repo's own `.claude/skills` (never the user's personal skills
in `~/.claude/skills`). So don't restate project conventions; spend the spec on what is specific
to this task: the files to read first, the existing code to imitate, and the skills to hand it
(below).

**The web is yours; the agent tests in a browser but doesn't browse.** Research (for a spec, an
agent's question, the model check) uses the browsing tools this session gives you, and the one the
user prefers where they have said (`~/.claude/CLAUDE.md`, `wb notes`, the repo's CLAUDE.md);
workbench brings none. A browsing preference the user tells you holds in every project, workbench
or not: offer to add it to `~/.claude/CLAUDE.md`, which every session reads (`wb notes` reach only
workbench mode, your memory only this project). Read the
installed source before the web, and a JSON API before a page (Hugging Face's `/api/models`,
GitHub's releases API): they rarely block a plain fetch. A 403, a CAPTCHA, a login wall or a page
empty without JavaScript means not checked: try another primary source, then say what you
couldn't verify; never fill the gap from memory or a copy. When a block cost an answer and this
session has no browser tool, say so in that report: a browser tool (Claude in Chrome, the desktop
app's browser, a browser MCP server) would reach it, and which is the user's choice. Put what the agent needs in the spec
(the signature, the few lines of docs), not a URL: its web tools are denied. A browser check of
its own work is a test: name one (Playwright, or the project's) that prints the console's and the
page's errors as text, and make it `--test` or part of it; screenshots are for you. A bare browser
(`chromium`, `google-chrome`, a global `lighthouse`) asks first, as `curl` does, since it fetches
any URL: allow it for the task, `--always` when the user wants it for every task.

**Your skills reach the agent as decisions.** Before writing a spec, check which skills and tools
in your own session fit the task, whatever the field: a design plugin for a UI, a writing guide
for docs, a security checklist for auth code, the user's own. Apply them yourself and put the
result in the spec: the decisions and the concrete rules this task must meet (the palette as
tokens, the terms and tone, the checks the code must pass), never the skill itself or "follow X":
the agent can't load your skills, and they are written for you. Review the diff against them too.
When the same correction from one of your skills comes up in a second task, it is a lesson: give
the agents a short library skill of just its rules for doing the work (`wb skill add`, or `wb
skill import` from the original when its license allows), handed out on the tasks it fits.

**Size every task to what the model that takes it finishes, as measured.** One concern, with the
files named, and no bigger than its slice: `wb models` (and `wb start`) give each model's, e.g.
`~3 files, ~150 new lines per task`. The slice comes from the context window the model is served
(on 32K, 2–3 files and ~150 lines, measured 2026-11-16; a task that spends it, 8 files at once,
cost 30–60 minutes of compactions, re-reading and drift) and is corrected by every step that
closes: the biggest that went well raise it, the smallest that went badly (compacted, stuck, or
two review rounds the agent caused) lower it; a handover to a fresh session doesn't count against
a step. So a strong
machine's big window earns big tasks; don't hold it to the 32K numbers. When the last few steps
went well, try the next one a little bigger than the slice; that is how it learns to grow.
Splitting is your job, the architect's: run the parts one after another, each with its own acceptance command.
A good split is logic, then its tests, then the UI: `wb next` gives each part to the same agent,
which already knows the code. (Measured: a step that never landed in an hour as one task landed
in ~30 minutes as three, every hidden check passing.)
- Names, signatures and constants that other code relies on: write them in the spec and say
  "exactly"; agents otherwise rename them to fit their own taste.
- The agent cannot see what it draws. For anything visual, look at the screenshot yourself;
  after one miss on a visual detail, fix it yourself rather than describing it again.
- Check the tests catch bugs, not just that they pass: break the code in a scratch copy a few
  ways and see each break fail a test.

## Skills for the agents: you write them, you hand them out
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

## Images: you art-direct, a local model draws
When the project needs an image (a sprite, an icon, a background, a README picture), and
`wb image --list` shows a ready engine, make it locally instead of asking the user for one.
Agents can't make images; one that needs an asset asks you: answer first (a placeholder now, the
asset at review), and make it once the GPU is free.
1. **Brief it like an art director.** Say what's in it, then the style, composition, palette and
   lighting, the size the project needs, and what to avoid. Name the use ("a 64x64 icon on
   a flat background") so the framing fits.
2. `wb image "<prompt>" --n 4 --size WxH [--seed S] [--ref img]` makes candidates (seeds S, S+1,
   ...) and prints their paths. **Read every one**: look at it yourself, never pick by filename.
3. Keep one, or rewrite the prompt for what was wrong and go again. A seed you liked comes back
   with `--seed` and a changed prompt. Stop after three rounds and show the user the best.
4. Copy the keeper into the project (resized or cropped as needed), commit it, and say which
   engine, prompt and seed made it (its `.json` sidecar has them). A project often ignores
   `*.png` for its screenshots: `git check-ignore -v <file>`, and add an exception for the
   asset directory. For a sprite on a black background drawn additively, black is see-through
   and no alpha channel is needed.
Mind the engine's `notes` in `wb image --list`: an image model's licence can restrict where and
how its output may be used. A generation needs the GPU to itself: while a task holds it,
`wb image` refuses and names it; `--wait` (Bash `run_in_background: true`) queues behind it.
While it runs (or waits), `wb start` refuses a new task (exit 4) unless `--queue`, which waits
behind it; feedback, next and resume queue behind it. A url engine is a server that keeps its GPU
memory while it runs: ask the user to stop it before big coding tasks.

**No ready engine? Offer to set one up; never set it up unasked.** This is an optional step, as
ARCHITECTURE.md's Images section (in the workbench code, above) says. Read `wb scout` for the OS and GPU, then propose in one
message: a runtime that fits (stable-diffusion.cpp on Vulkan for AMD or Intel, CUDA for NVIDIA,
Metal on macOS; built from its upstream source, with packages from the OS's own repositories),
and an image model you've just checked. It must fit the VRAM without spilling, and its licence
must allow the user's use and country. Some licences exclude whole regions, so read the licence
text, not the tag. Give the download size. Wait for a yes, then install, write
`~/.config/workbench/images.json` (the licence goes in `notes`), make one test image, and look at
it before saying it works.

## 3. Dispatch
```
wb start <kebab-name> --spec <file> --repo <repo> --size significant|trivial \
   --test "<acceptance command>" [--skill <name>] [--model …] [--auto-fix 2]
```
It returns at once; a detached runner does the work. **Don't poll.** With the workbench mod loaded
the session is woken by a `[workbench]` prompt when the task needs you (review, question, stuck,
failed) — end your turn and wait for it. Without the mod, run `wb wait <name>` with Bash
`run_in_background: true` and act on its notification. If the context says wakes are off (the
plugin's wake setting), none will come: tell the user it runs, and wait only if they ask.
A session is woken only for its own tasks (those `wb start` ran in it, wherever; one started by
hand, or one whose Claude Code process has exited, from its directory or a repo in it): start a task from the session that will review it. Failing tests go back to the agent on their
own for `--auto-fix` rounds before you are woken; `wb log <name>` shows what it is doing.
A model that reads prompts slowly, or one the runner must load first, can spend minutes before
its first reply. That is normal, not stuck.
**Exit 4: the GPU is taken** by another task (often another session's project) or `wb image`,
which the model can't run beside. Don't wait silently: tell the user who holds it, then either
do the step yourself or `--queue` it to start when the GPU is free (it may be hours).

## Driving a running task
You are the driver, not a spectator. When something tells you the agent is heading the wrong way
(a check-in, its log, the user), act on it:
- `wb steer <name> --text "…"`: a note it gets as soon as its current run ends, before the
  tests. For course corrections that can wait a step.
- `wb steer <name> --now --text "…"`: interrupts it mid-step; the same session continues with
  your note first, marked high priority. For wrong files, a wrong approach, a runaway loop.
  While its tests run or it waits for the GPU there is nothing to interrupt: the note waits, and
  `wb steer` says so.
  The runner does this by itself once a round when the agent generates past its thinking
  budget with no tool call (7 minutes from its first output; `--think-timeout` overrides),
  and the check-in says so; a round that stalls again right after is stopped. Two nudges on one
  task mean the spec is too big for one step: split it, or name the first file to write.
- A note the runner never delivered (the round stopped first) is in the stuck report. `wb resume`
  carries it; `wb feedback` and `wb next` drop it and say so: put it in the feedback if it still
  matters.
- `wb takeover <name>`: stops it and hands you the worktree. Finish by hand, leaving it
  uncommitted (`wb accept` commits it), then `wb accept`, or hand it back with `wb feedback`. For when explaining would cost more than doing.
- **Commands it asks for.** The agent's shell runs the everyday tools of software work freely;
  anything else, and adding a package, is refused, and the agent asks you (a `PERMISSION`
  question). Lean to yes: their work is hard enough. `wb allow <name> "<pattern>"` (e.g.
  `"curl *"`) grants it for the task, answers the request and restarts the agent on its session
  with the rule; `--always` keeps it for every task (`~/.config/workbench/commands.json`). Say no
  (`wb answer`, with another way) to what reaches outside the task: deploys, other machines,
  system services, the user's data, a dependency the project shouldn't take on. Knowing a task
  needs something up front, `wb start … --allow "<pattern>"`. sudo, git writes, git by its full
  path and `gh` stay refused whatever you allow. A plain `npx <pkg>` runs only what the project
  installed; to let it fetch one, allow `npx --yes <pkg>`.
- **Directories it asks for.** The agent writes only in its lane (worktree, temp, build caches)
  and its file tools keep to it; a request names a `path`. `wb allow <name> --path <dir>` grants
  read and write (`--always`, `wb start --allow-path`). Grant a directory the task's output
  really belongs in (a sibling asset folder, a cache a toolchain wants); never your home, the
  user's checkout or another project: there, take the file yourself or say no. `wb allow --path`
  refuses a path through a link (it names the target: allow that only if it is what the task
  needs; the agent can plant links in its lane), a place inside credentials or workbench's own
  state and config, and one in the task's own repo (the user's checkout: take that file yourself).
  A toolchain the build wants installed (a Python uv must download, a
  `rust-toolchain.toml` toolchain, `uv tool install`, `cargo install`) is refused there: install
  it yourself rather than grant `~/.local/share/uv` or `~/.rustup/toolchains`, whose programs
  run later outside the lane. A test driving a program through a pseudo-terminal (pexpect, Python's pty,
  `script`, node-pty) fails in the lane ("out of pty devices", "failed to create
  pseudo-terminal"): grant `/dev/pts` (`wb start --allow-path /dev/pts`), which also opens the
  user's other terminals to the agent.
- `wb stop <name>`: just stop it. `wb resume <name>` continues it in a fresh session (not a
  review round), e.g. after fixing workbench under it. A task failed with "the runner died" (a
  reboot, a kill) or stuck with "stopped by a signal" resumes the same way.
- A check-in saying the session "reached N% of its window before it changed a file" (or "had
  read N% before its first change": a fresh session would have little room after re-reading it):
  the step needs more reading than the window holds. Let it carry on; next time name fewer files to read,
  or put what it needs (a signature, a constant) in the spec.
Notes are instructions, like feedback: concrete and short.

**The user's two commands.** Only these, never words in a message, stop the delegation and end
workbench mode; the mod runs them, closes the pane and tells you what it did:
- **`/workbench-quit-and-hand-back-claude`**: take over. This session's agents are already stopped
  (`wb takeover --all --repo <here> --session "$CLAUDE_CODE_SESSION_ID"`). Leave workbench mode and finish the work yourself without pausing: review
  what each agent left, complete it in its worktree to your standard, run the acceptance command,
  `wb accept`, then carry on with the rest of the request directly, without delegating.
- **`/workbench-quit`**: stop. This session's delegated agents are already stopped (`wb stop
  --all --repo <here> --session "$CLAUDE_CODE_SESSION_ID"`). Work directly from now on. Don't finish, restart or clean up anything
  unless asked; worktrees are kept.
Tasks listed as left alone belong to another live session: leave them unless asked.
Without the mod, the user may ask for the same in words; run those commands then.

**The web chat.** While workbench mode is on in a session, the user can reach you from their phone
(`wb chat serve` on port 1337; the link is localhost, and reaching it from a phone is the user's own setup). Their message arrives as a prompt that is
just `[web chat] <their words>` (or, while you're mid-turn, delivered into the turn by the mod,
with one line saying so); it has the same authority as one typed here. They read your answers on a phone, so
reply there with `wb chat reply --text "…"`: a one-line acknowledgement first when the work will
take a while, then the outcome, short (what was done, what needs them). Don't stream progress or
paste diffs. While a `web-chat` context block says they are away, also tell them there when
something they asked for finishes or needs them. `wb chat log` shows the thread. The web thread is
all they see: each reply must stand on its own, and a yes there answers only a question asked
there, never one asked in the terminal. Nothing hard to undo (downloads, deletes) on an ambiguous OK. A message they
type in the terminal means they are back.

**Check-ins.** A significant task wakes you every 10 minutes while it runs (`--checkin MIN`
changes it, `0` turns it off) with a digest: tools used, errors, tokens, files touched, its latest
words, time so far and the ETA. Be the captain: give the user two or three plain lines on how it
is going, log them with `wb note <name> --text "…"` (they show in `wb watch` and the pane), and
steer only if it is drifting. Say what you see, including "slow but on course". Keep it short;
every check-in costs your tokens.

## 4. When woken
- **review**: the report gives the test result, the agent's summary and the files. Read
  `wb diff <name> --stat`, then the full `wb diff <name>` (`wb diff <name> <path>` for big
  ones). Read every line. Check:
  - **Correctness**: trace the logic, including edge cases, error paths and inputs the tests
    don't cover.
  - **The spec**: every requirement met, nothing missing, nothing added.
  - **Tests**: they test the behaviour and would fail on a bug; none weakened, skipped or
    special-cased; expected values not just copied from the output.
  - **Fit**: the repo's conventions, naming, idioms and existing helpers; no duplicated code.
  - **Hygiene**: no stray files, debug output, dead code or TODOs.
  - **Safety**: injection, secrets, unsafe file or shell use.

  If anything is doubtful, check it yourself: run the tests or a quick probe in the worktree
  (`wb status <name>` prints its path); a UI change, run it from the worktree and look with your
  own browser tool. Accept only what you would have written.
- **question** — answer it briefly and decisively: `wb answer <name> --text "…"`. The agent is
  blocked until you do.
- **stuck / failed** — `wb log <name> -n 30` if the report isn't enough; then feedback that unblocks
  it, or discard.
- **After every review**, one question: did I just correct something the next agent will get
  wrong too? The `lessons:` line and `wb lessons` put the candidates in front of you; turn each
  into a skill or close it (above), now, while the lesson is fresh.

## 5. Feedback
Numbered, concrete changes in a file → `wb feedback <name> --message <file>` (or `--text "…"`). Small models need
explicit instructions ("in parse(), return 0 for an empty string"), not critique. Send back
everything that falls short in one round, small things included. A nit you let through is a
standard you just lowered. Confirm or correct each assumption the summary lists, in the same
message: an assumption left unanswered comes back as a question, and the agent waits for it.
When the fix is for something your spec missed, add `--spec-gap "…"` (checklist item it broke).
**Max 3 review rounds.** Still wrong after that: make the small remaining fix yourself in the task's worktree
(`wb status <name>` prints its path) and accept, or `wb discard <name> --reason …`.
If a "trivial" task fails twice, it wasn't trivial: discard and restart it with `--size significant`.

## Context, on both sides
**The agent's.** Rounds of a task continue one OpenCode session, so the agent keeps what it read.
When that session passes its handover point (60% of the window until `wb tune` learns better
for that model; less on windows under ~32K, and a model served under 16K can't take tasks), the runner hands the task to a fresh session
at once, mid-round if need be: the task, the work so far (the diff), the agent's last words and
the new instructions. A round that hands over 5 times is stopped as stuck (the step is too big for
the window: split it), and from the second handover each one is a check-in. OpenCode's own compaction (the model summarising itself, which can lose the
task) is only a backstop past that. Reports show `agent context: …` so you see it coming. A
check-in or report that says `NOTE: the spec doesn't name …` lists files outside the spec: drift
until you've looked, so read them and steer (`--now`) if the agent left its task. `NOTE: <dir>: a
git repository of its own` is a nested repo the diff leaves out and `wb accept` refuses: have its
`.git` removed, or gitignore it. Override per round:
`wb feedback --fresh` when the agent is confused, stuck on a wrong idea or working from stale
reads; `--keep-context` when it needs what it read and the window still has room.
Follow-up work in the same area goes to the same agent: `wb next <name> --spec <file> -m "<subject
for the approved step>" [--test …]` commits the approved step on the branch and sends the next
step to the same worktree and session (`--fresh` for a new one). `wb diff` then shows the current
step, `--all` the whole branch; finish with `wb accept`. Unrelated work is a new task with a fresh
agent. A new task can't inherit an old task's session: OpenCode binds a session to its directory.

**Yours.** `wb` holds the delegation state, so your context doesn't have to. When a task closes and
none waits for you, the mod compacts you once you are halfway to automatic compaction, keeping
the user's goals, the tasks and what each needs next (`WB_COMPACT_AT`, a percentage, 0 turns it
off); any compaction keeps that state, and after a compaction or `/clear` a `workbench` context
block lists the open tasks. So don't carry diffs or reports forward: `wb status` and
`wb report <name>` bring them back. Between unrelated jobs, tell the user `/clear` is safe.

**What you learn about the user.** When the user tells you how they want delegation run (a
review bar, how much to ask them, what to merge, a style they always want), and it would hold in
any project, `wb notes add "<it, in one sentence>"`. Your own memory stays in one project; these
notes come back in every session in workbench mode (a `user-notes` block). A fact about one repo
goes in that repo's CLAUDE.md instead. Drop a note that has stopped holding (`wb notes drop N`).

## 6. Finish
- Approved: `wb accept <name> -m "<conventional commit subject>"` commits on `delegate/<name>`
  with the task and OpenCode session recorded. Add `--merge` only if the user passed `--merge` or
  said to merge; otherwise give them the merge command it prints.
- `wb discard <name> --reason "…"` drops it. `wb status` lists open tasks (e.g. after a context
  reset); `wb stats` compares the models on finished work.

## Report to the user
Short and honest: what was built, on which model, rounds (automatic and review), test result,
what you fixed, sent back or flagged, the branch. Report the agent's misses plainly; they are
evidence about the model, and the router learns from accept and discard. Mention the local tokens from the report next to the rough size of
your own review, so the saving is visible.
