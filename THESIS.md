# Half the Claude, the same result

**The claim.** If you have a gaming GPU and cheap electricity, you can get Claude's results on
bigger coding tasks for about half the Claude tokens. Claude still writes the spec and reviews
the work. A local model on your GPU writes the code. The result passes the same tests, and the
electricity costs a few cents.

This page shows how we tested that, what we measured, and where it doesn't hold. The raw numbers
are in [ARCHITECTURE.md](ARCHITECTURE.md#what-a-handoff-costs-claude-measured).

| | |
|---|---|
| **When we started** (2026-11-17) | a handoff cost Claude **1.6–4.5×** what writing the code itself did |
| **Bigger task, fresh session** (0.93) | **0.82–0.90×**, counting the session's own startup costs |
| **Bigger task, session already in use** | **about 0.4×**: 48K tokens against 116K |
| **Small task** | **1.1–1.5×**, so Claude hands small ones over in batches |
| **Electricity for a bigger task** | **under 5 cents** (a 317 W card for 6–30 minutes) |

## Why it can work

Most of what a coding task costs Claude is not the code it writes. Each step, Claude re-reads
the whole conversation: the files it opened, the test output, its earlier attempts. A task that
takes 20 steps pays for that growing context 20 times. Output tokens also cost about five times
as much as input.

When Claude hands a task off, it does only what it is best at:
1. It writes a precise spec: what to change, which names other code relies on, and a test that
   proves it's done.
2. It reads one finished diff and signs it off.

All the reading, the trial and error and the test runs happen in the local model's context, on
your GPU, where tokens cost electricity.

That only pays off if the handoff stays cheap. At first it wasn't: workbench woke Claude for
check-ins, warnings and handovers it didn't need, and Claude fixed in review what the local model
should have caught. Getting from 4× to under 1× took one night of measuring and about 35 small
pull requests (#28–#62).

## How we tested it

Every number here comes from a **paired run**:
- **Same request, both ways:** one run had Claude write the code itself, the other had Claude hand
  it off through workbench. Both ran as Claude Code subagents on the same Claude model and
  settings, at the same point in a session.
- **Graded by hidden tests:** each task had tests that neither Claude nor the local model saw.
  Every result reported here passed them. A cheaper run that broke the code would not count.
- **Claude's tokens, weighted by price:** input 1, cache write 2 (the 1-hour cache a main session
  uses), cache read 0.1, output 5. That gives one number in "input-equivalent" tokens.
- **The tasks:** real features in two projects. In a C game: saving and loading replay files, and
  scoring combos (both significant), plus ranking a high score (trivial). In a Python report tool:
  a date range filter (trivial).
- **One machine:** an AMD Radeon RX 9070-class card with 16 GB, running a 27B open-weights coding
  model, 3-bit quantised. `wb optimize` found that its 40K-token window is the largest that stays
  entirely on that card.

## What we measured

The final setup, as shipped in 0.93:

| Task | Claude writes it | Handed off | |
|---|---|---|---|
| Save and load replay files (C, significant) | 166K | 150K | **0.90×** |
| Score combos (C, significant) | 176K | 145K | **0.82×** |
| A date range for a report (Python, trivial) | 50K | 67K | 1.34× |
| Rank a high score (C, trivial) | 54K | 83K | 1.54× (one round sent back) |

Those significant pairs each start a fresh session. Both sides pay the session's base context
(about 50K), and the handoff also pays for Claude reading the workbench skill once (about 29K).
In real use you pay both once per session, not once per task. Take them out and the handoff for
replay files cost Claude about **48K** (9K to send it, 31K to review it, 8K to accept it). Writing
it cost about **116K**. That's the "half".

How it got there, on the same two significant tasks:

| Setup | Replay files | Score combos |
|---|---|---|
| Start: 32K window, the early skill | 2.5× (5 compactions) | 2.1× (7 compactions) |
| 64K window | 1.55× | 1.3× |
| 40K window entirely on the GPU, lean sign-off, self-review | 1.18× → 1.40× → 1.38× | **0.82×** |
| The agent proves each check it wrote by breaking it | **0.90×** | |

## What made the difference

- **The local model reviews its own work.** Before Claude sees the diff, the agent checks it
  against the spec and the project's rules. For each check it wrote, it comments the check out,
  watches a test fail, and puts it back. Claude then signs off in one call instead of sending the
  work back.
- **The request is the spec.** Claude had been rewriting precise requests into specs as long as
  the code. Now a precise request goes to the agent as written.
- **No wasted wakes.** Periodic check-ins, repeated warnings and handover notices each woke
  Claude over its whole context with nothing to do. They're now off by default or a line in the
  report.
- **The diff comes with the wake.** Claude doesn't spend calls fetching it, and after feedback it
  sees only what changed since its last review.
- **The right window for the card.** A window that spills into system RAM halves the writing
  speed. One too small makes the agent compact and lose its place. `wb optimize` measures the
  largest window that fits on your card, so this setting comes from your hardware.

## What it costs you

On the test machine the card draws **14 W idle** and **290–317 W while the model writes**
(measured from the card's own power sensor, at about 38 tokens a second). A significant task
kept it busy for 6–30 minutes, so 0.03–0.16 kWh. At $0.30 per kWh that's 1–5 cents, less where
power is cheap. The rest of the computer draws something too, which we didn't measure.

On Claude's side, a warm significant handoff saved about 68K input-equivalent tokens. At the API
list price of Claude's default model when this was written ($4 per million input tokens), that's
roughly 25 cents per task, five times the electricity or more. On a Claude subscription, it means
your usage limits go up to about twice as far on this kind of work.

You also need the hardware: a GPU with about 16 GB, such as the one above, runs a 27B model at
full speed. A smaller card runs smaller models, which take smaller slices of the work.

## Where it doesn't hold

- **Small tasks cost about the same or more.** Telling a model what to do and checking it costs
  about what doing it would. Claude batches small requests into one handoff for this reason.
- **A wrong first attempt is expensive.** A round sent back costs Claude about 25K, roughly
  doubling a small task. Every fix above cuts how often that happens; none removes it.
- **Few runs, one machine.** This is four tasks, about fifteen handoff runs over one night, on one
  card and one model. The same task varies by about 0.2× from one run to the next. Treat the
  numbers as a demonstration, not a benchmark.
- **It's slower by the clock.** A significant task took the local model 6–30 minutes. The point is
  Claude's tokens, not your wall-clock time. You can keep working with Claude meanwhile.
- **The GPU is busy.** While an agent works, the card isn't free for games or other models.

## Try it on your own work

Install workbench (the [README](README.md#quickstart) has the steps), run `/workbench` in Claude
Code, and give it a real feature. `wb optimize` sizes the window for your card on its own, and the
router learns from each task you accept which model suits which size of work. A bigger GPU or a
stronger local model shifts every number above in your favour, and local models keep improving.

You see the result on your own tasks. `wb accept` prints what each one cost Claude beside an
estimate of Claude writing it, and the GPU energy it used. `wb savings` adds up the week (or
`--month`, `--all`), and the Workbench pane and the web chat's settings show the same total. The
estimate is fitted from the paired runs above, so treat any single task as within about half
either way, and a week of tasks as the better guide.
