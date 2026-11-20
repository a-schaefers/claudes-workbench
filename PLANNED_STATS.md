# Planned: a Stats tab

2026-11-20. A plan, no code yet. It describes a third tab in the mod's pane, **Stats**, with two
halves: what this machine is doing right now (an htop-like live view), and everything workbench
has learned over time (history). It also says what the Tasks tab keeps, what moves and what is
shown in both, which opt-in measurements become defaults, and how none of it costs anything while
the tab isn't on screen. When the last phase ships, the design moves into ARCHITECTURE.md and
this file goes, as FINDINGS.md did.

Source lines are at origin/main b1fe256.

## Why

Everything workbench does is measured somewhere: every closed task's row in `results.jsonl`,
each model's speeds, the GPU energy, the savings estimate. Almost none of it is on screen. The
pane shows the tasks of the moment and one line of the week's saving. `wb stats` prints a table
nobody opens. A user who has run a hundred tasks can't see that the agents fixed their own test
failures forty times without waking Claude, or that their main model got 15% faster after
`wb optimize`. Seeing that is the encouragement: the setup is working, and this is what it did.

The live half comes from a different need. While a task runs, the user wonders whether the GPU is
busy, which model is loaded, how fast it writes, and what's waiting. The owner pointed at a
desktop app shown on r/LocalLLaMA (screenshot, 2026-11-20). It was a "local model analog
control" panel with:
- a status lamp and the generating model with its first-token time;
- three gauges: generation speed (tok/s of the last request), GPU memory (used of total) and GPU
  activity (%);
- a "model rack": one channel per model, each with its expected tok/s, its load time and a
  loaded LED.

The ideas are good and the form doesn't fit: we draw text in a terminal pane. So this plan takes
what matters (what's loaded, how fast, how full, what's waiting) and draws it the way htop
does: bars, numbers, one row per thing. It copies none of that app's look.

## The rules it keeps

- **Honest.** Every estimate says so, as the savings line already does (its fit's mean error is
  19%). No number is shown that wasn't measured or derived from what was. A source that doesn't
  exist on this machine shows as "not measured", never as zero.
- **Local.** Nothing leaves the machine. Every source below is a file under `$WB_HOME`, sysfs, a
  local process, or a model server this machine already talks to.
- **No new wakes.** Nothing here wakes Claude or spends Claude's tokens. Claude's context never
  sees the Stats tab.
- **Free while hidden.** With the tab not on screen there is no new process, timer or read
  (*Performance*).
- **No model named.** Labels come from OpenCode's config and models.json, as everywhere else. The
  mock-ups below use placeholders.
- **Every machine.** Linux, WSL2 and macOS, any GPU vendor, or none. What a platform can't
  report is left out of its row, not guessed (the *Design for all hardware* rule).

## Where it lives: a third tab

`Tasks · Stats · Settings`, keys `1`, `2`, `3`. The minimized strip gains `Stats` beside `Tasks`
and `Settings`. A tab rather than a second pane:
- the engine shows one pane at a time and puts the others behind it as tabs anyway;
- the tab pattern already exists (`TABS`, `mod/hooks/register.tsx:131`; the `pane-1` atom
  `{tab, open, focus, minimized}`);
- one pane keeps one place to look.

The Stats view counts as **shown** when `pane.tab === 'stats'` and `$.ui.panes()` says the
Workbench pane `isShown` (the check `refreshLive` already makes, `register.tsx:776`). Minimized,
closed, behind another pane, or on another tab, it is not shown.

Settings moving from `2` to `3` is a small break in habit. The owner can choose instead to put
Stats last (`Tasks · Settings · Stats`) and keep `2`. This plan puts Stats in the middle because
it is looked at more often than Settings.

## The Stats tab

Two halves, live on top and history below, so the part that changes is where the eye lands
first. Both are text: bars with `▏▎▍▌▋▊▉█`, sparklines with `▁▂▃▄▅▆▇█`, colours from the theme
(the `LINE_STYLE` set). No chart element exists in the plugin API, and none is needed.

### Now (live)

```
 GPU 0  VRAM ███████████████░░░░░  18.2/24.0 GiB   busy ████████░░  81%   287 W   ▃▅▇▇▆▇█▇
 NOW    add-scores · model-a · writing · 41 tok/s (median 38) · round 2 · 4m12s
        read 1.1K tok/s · first token 6.2 s (last call) · context 31K of 64K (48%)

 RACK   ● model-a     on GPU  17.9G         write 38   read 1.1K   load ~45s   ← add-scores
        ○ model-b     idle                  write 61   read 1.4K   load ~2m
        ◑ model-c     on GPU  9.1G + CPU 4G write 12   read 400    load ~1m
        ✗ model-d     can't run: server down
        ⇢ model-e     another machine       write 29   read 900    no telemetry
 QUEUE  fix-typo waits for the GPU (1m20s) · then docs-pass
```

- **GPU line.** One per GPU:
  - VRAM used of total, as a bar;
  - busy %, as a bar;
  - watts;
  - a sparkline of busy % since the tab opened, kept in memory only.
  A field the platform can't give is left out: Apple has no VRAM-used figure, and WSL without
  NVIDIA has no busy %. A machine with no GPU says `CPU only` and shows RAM in its place.
- **NOW.** One line per running task: task, model label, phase, tok/s, then round and time.
  - **Live tok/s** where the server reports it: `/metrics` with a `live` object, already read by
    `transcript._server_live`, `workbench/transcript.py:363`.
  - **Otherwise the last finished call's tok/s**, from its `step` event in `events.jsonl`,
    labelled `(last call)`.
  - The median beside it is from `speed.json`, so the user sees at once whether this run is slow.
  - Read speed, first token and context fill go on the second line. The context fill is the same
    figure as the Tasks tab's clock.
  - With nothing running: `idle · last task landed 12m ago`.
- **RACK.** One row per candidate model, in the router's order.
  - **State mark.** `●` resident on the GPU, `◑` split between GPU and CPU (Ollama's `/api/ps`
    gives `size` and `size_vram`), `○` idle, `✗` can't run (the reason `wb models` gives), `⇢` on
    another machine.
  - **Speeds.** Median write and read tok/s (`router.speed`, `workbench/router.py:299`).
  - **Load.** The typical load time: the median of tasks' `load_secs`, computed rarely and cached.
  - **Holder.** The task holding the GPU with it (`← add-scores`).
  This is the screenshot's rack, as rows.
- **QUEUE.** The GPU lease's waiters, oldest first, as `GpuLease` orders them (`gpu.json`'s
  `waiting` entries; `models.holders`, `workbench/models.py:1272`, drops dead pids).

### History

```
 THIS WEEK   score 62/100 · saved ~1.2M Claude tokens (estimate) · 9 tasks · 8 landed · 1.4 kWh
 ALL TIME    131 tasks · 112 landed (85%) · saved ~14M (estimate) · 21.6 kWh ($6.48)
 STREAK      5 days in a row with a task landed · best 11

 THE AGENTS ABSORBED
   48 test failures fixed without waking Claude · 17 questions answered mid-task
   9 handovers to a fresh session that went on to land

 BY MODEL            tasks  landed  rounds  minutes  write tok/s        claude/task
   model-a              84     88%     1.4       14  38  ▅▅▆▆▇▇▇█ +12%       41K
   model-b              31     77%     1.9       22  61  ▆▆▆▆▆▆▆▆            55K
 BY SIZE   trivial 22 (100%) · small 61 (90%) · medium 38 (79%) · large 10 (60%)

 LAST 30 DAYS  ▁▃▂▅▇▃▁▁▄▆▅▇█▃▂▁▅▆▄▃▂▅▇▆▄▁▁▃▅▆   tasks per day
 BESTS         largest landed first try: 412 lines (add-csv-export) · fastest: 2m (fix-typo)
 WHAT STOPS AGENTS   repeated call 6 · tool errors 4 · silence 2 · no edits 1
 WHAT HELPS    a hint helped 3 of 4 · a smaller step 2 of 2   (progress checks)
 BENCH         model-a 7/8 · model-b 6/8   (last run 2026-11-18)
 OPTIMIZED     model-a 64K all on GPU · model-b 32K   (2026-11-19)
```

- **Headline.** The savings badge's score and sentence (`savings.score`), with the period's
  totals for this week, this month and all time (`savings.PERIODS`). Energy shows always, and
  its cost once a price is set: the row says how (*Settings*).
- **Streak, bests, milestones.** Days in a row with a landed task, and the best run. The largest
  change landed in its first round, and the fastest landing. A milestone gets one line the day
  it is reached: first task landed, 10, 100, the first 1M tokens saved. Nothing says a streak was
  broken, and nothing counts down. The point is to show what went right, not to nag.
- **The agents absorbed.** Work that never reached Claude:
  - auto-fixes that turned failing tests green;
  - `ask_reviewer` questions answered;
  - handovers after which the task landed.
  This is the most direct evidence that delegating is working.
- **By model.**
  - The table `wb stats` prints today: tasks, accepted %, rounds, minutes, and Claude's cost per
    task (`results.table`, `workbench/results.py:94`).
  - Plus a write-speed sparkline over time and its change, which needs timestamped speeds
    (*Recording*).
- **By size and stack,** and tasks per day over the last 30 days.
- **What stops agents.** The guards that fired, from `stopped` lessons. **What helps:**
  `progress.helped` (`workbench/progress.py:57`), shown once there is any.
- **Bench and optimize.** Each model's last bench score (`bench.rows`) and its fitted window
  (`optimize.json`). Skills: each handed skill's outcome record.
- A section with nothing in it is left out. A new install shows the headline's "nothing until a
  task can be compared" and one line on how to start, not a page of zeros.

## Tasks tab: what stays, moves or shows in both

One expert read all of `register.tsx` and judged each thing the Tasks tab draws. The principle:
- **Tasks is what needs attention now**, per task and actionable.
- **Stats is aggregate, historical and machine-wide.**

| Item | Where | Why |
|---|---|---|
| Task row: mark, name, phase, round, model or label, tests, age | Stays | Actionable now |
| Recent events, `last`, the `allowed` coding exception | Stays | Per task, actionable, a permission in force |
| Live transcript (`refreshLive`) | Stays | Its core purpose; already skipped while hidden |
| Apprentice, bubble, "who acts next" | Stays | The attention signal |
| Clock: task and round time, context % of the window | Stays | Whether to step in |
| Status line, minimized strip, toasts | Stays | Attention signals; no stats there |
| Clock: ETA "(typically Nm)" | Both | The ETA stays; Stats shows the typical minutes behind it, per model and size |
| Clock: model load time, tokens written | Both | This task's figure here; medians and rates in Stats |
| Meta line (reasoning, variant, context) | Both | The followed model here; every model in the rack |
| `wb optimize` toast | Both | The toast as now; the result stays in Stats |
| Weekly saving footer (`savedLine`, `register.tsx:869`) | Both, shorter | One line stays in Tasks (`saved ~1.2M this week · Stats ›`); the breakdown moves |
| `WbTask.tokens` (read at `register.tsx:670`, never drawn) | Moves | Becomes the live rate in Stats |
| Closed tasks after their 30 minutes in the list | Moves | They become history |
| Everything `wb stats` prints | Moves | Now on screen, in Stats |

The Tasks tab doesn't get busier. It loses nothing a user acts on, and gains a way into Stats.

## Opt-ins: what becomes a default

The owner's rule: what is opt-in today should be on by default unless it has a real cost, and
anything on can be turned off in Settings. Each opt-in, checked:

| Setting | Today | Cost | Plan |
|---|---|---|---|
| **Stats recording** (new: the fields under *Recording*) | n/a | A few bytes per results row and event; no process, no model call | **On by default**, Settings row |
| **Live view** (new: `wb top` while the tab is shown) | n/a | One small child process, and only while shown | **On by default**, Settings row; off, the tab shows history only |
| Progress checks (`wb progress on`, `progress.json`) | Off | Each check is a `checkin` notice that wakes Claude, who rereads the whole context | **Stays off**, gains a Settings row so it can be found. Its measurement half (`active_s`, interventions) already records when off and feeds Stats. |
| Check-ins (`--checkin MIN`) | Off, per task | The 10-minute default cost 500K of one task's 1.2M tokens (2026-11-17) | Stays off, per task, as now |
| Web chat (`wb chat on`) | Off | A listening HTTP server | Stays off |
| Push notifications | Per device | An empty POST to the browser vendor's push service (`PUSH_HOSTS`, `workbench/webpush.py:33`) | Stays per device |
| Away "allow" (`chat/prefs.json`) | Ask | Permissions granted with no one there | Stays ask |
| Electricity price (`kwh_price`) | Unset | None, but it can't be guessed | Unset; Stats shows energy without it, and a Settings row takes the price |
| Self review, early compaction, optimize when due, weekly model check, weekly update check | On | Already weighed | No change |

Nothing turned on by this plan wakes Claude, sends anything off the machine, or holds the GPU.

### Settings rows

These are new rows in the Settings tab, in its existing style (`settingRow`,
`register.tsx:366`):
- `▸ Stats recording: On (default)`, with options `On` and `Off`.
- `▸ Live view: On (default)`, with options `On` and `Off`.
- `▸ Live refresh: 2 s (default)`, with options `1 s`, `2 s` and `5 s`.
- `▸ Progress checks: Off (default)`, with options `Off` and `On`.
- `▸ Electricity price: not set`. This row is an Input: a number per kWh.

Each pick runs a `wb` command, as the coding and chat rows do; the mod writes no setting itself.
The commands:
- the first three: `wb stats set recording|live on|off` and `wb stats set every 1|2|5`, all
  writing `$WB_HOME/stats.json` `{recording, live, every}`;
- progress checks: `wb progress on|off`;
- the price: `wb savings --price X`.

With no file the settings are the defaults, so an install that never opens Settings gets
everything above.

## Recording: the gaps to close

History only exists from the day it is recorded, so this goes first. What isn't kept today, and
the cheapest way to keep it:

- **Results row, new fields.**
  - `started_at`.
  - `load_secs` (now only in the task's `status.json`).
  - `nudges`, `asks` (count), `guards` (which fired: `{repeat: 1, silence: 1}`), `setup_secs`.
  - The task's speeds: `speed {write, read, ttft}`, medians of its calls. These come from the
    same session export `measure_speed` already reads after each round
    (`workbench/runner.py:1458`), so no new export.
  - Watts `gpu_w {mean, peak}`. The runner's `_Power` thread (`runner.py:798`) samples every
    `POWER_EVERY` 10 s and keeps only the sum; it would keep the mean and peak too.
  Each field is a few bytes on a row written once per task.
- **A `round_end` event** with the round's duration and how it ended. Today a round's length is
  only inferred from the next event.
- **Speed over time comes from the rows**, which now carry their own speeds and `ts`.
  `speed.json` stays what it is: the router's last 60 samples per model (`SPEED_KEEP`,
  `router.py:264`), no timestamps.
- **No continuous time series on disk.** VRAM, busy % and watts are sampled only while the live
  view is shown and kept in memory for its sparklines. Sampling them all day, for a chart nobody
  opened, would be the cost this plan promises not to add.
- **Stats recording off** writes none of the new fields. The fields the router, tuning and
  savings already rely on are not part of this setting and keep being written.

The rows stay readable by older code (new keys only). `wb stats --json` treats a missing field
as "not recorded", so rows from before this change still count where they can.

## The feeds

Two `wb` commands, so the numbers come from the Python that already knows them, and the mod
just draws.

**`wb stats --json`: history, on demand.**
- Built from:
  - `store.results()`, which leaves out trials;
  - `savings.totals` and `score`;
  - `router.score`, `speed` and `expected_minutes`;
  - `tuning.scores`, `progress.helped` and `bench.rows`;
  - the `stopped` lessons and `optimize.json`.
- One object: `{periods, streak, bests, milestones, absorbed, models, sizes, days, guards, helps,
  bench, optimized}`.
- `wb stats` without `--json` stays the text table, extended with the same sections.
- The mod runs it when the Stats tab is first shown in a load, and again when `results.jsonl`
  changes while the tab is shown (`wb events` already reports it). It caches the answer in a new
  `stats` atom (shape `stats-1`).
- No timer. Once read, the history half costs nothing until a task closes.

**`wb top [--json] [--every S]`: live, only while shown.**
- One long-lived child, like `wb events`: one JSON object per tick (default every 2 s).
- The mod spawns it when the Stats view becomes shown and kills it when the view stops being
  shown. Each line becomes one write of a `top` atom (shape `top-1`).
- What it reads, cheapest first:
  - **sysfs**, for VRAM (`mem_info_vram_used`/`_total`) and busy % (`gpu_busy_percent`) on
    AMD and others: file reads of microseconds. New readers in `hardware.py`, which today reads
    VRAM only as part of `gpus()` (which also runs `lspci` per card) and never reads busy %.
  - **NVIDIA**: one `nvidia-smi --query-gpu=memory.used,memory.total,utilization.gpu,power.draw
    --format=csv -lms <every>` child, started with `wb top` and ended with it. Starting
    `nvidia-smi` on every tick would cost a driver load each time.
  - **Watts** elsewhere: `hardware.gpu_power_w` (`hardware.py:211`), as the runner's sampler
    reads it.
  - **Tasks**: `gpu.json` (holders and the queue), `speed.json`, and the running tasks'
    `status.json` and newest `step` events.
  - **Servers**, every 5 s, not every tick: Ollama `/api/ps` (residency, GPU/CPU split), each
    server's `/metrics` for a `live` object, and `/v1/models` for others.
  - **Never** `opencode session export` (the expensive call `wb watch` makes) or anything that
    loads a model.
- In a terminal with no mod, `wb top` without `--json` is the same view drawn in place with ANSI
  (as `wb watch` rewrites its activity row): the htop-style screen for anyone without the
  plugin. `q` or Ctrl-C quits.

## Performance

The owner's condition: no cost to the application, and no use of resources while the tab isn't
looked at.

**Hidden** (another tab, minimized, closed, or behind another pane):
- no `wb top` process;
- no new timer;
- no `wb stats` call;
- no read of anything new.
The existing poll is unchanged. The test: `register.test.tsx` checks that no `wb top` is spawned
while the tab is hidden, and that the child is killed when the user leaves the tab or minimizes
the pane.

**Shown:**
- `wb top` at 2 s does a few file reads per tick and one HTTP call per server every 5 s. The
  budget is under 1% of one CPU core; measure it with `time` over a minute before shipping.
- Each tick is one atom write. The engine redraws a shown pane at most 30 times a second, and a
  tick every 2 s is far below that.
- The Stats text stays a few thousand characters, far below the pane's 100K limit (the reason
  for `HISTORY_CHARS`, `register.tsx:111`).

**Headless sessions** (`claude -p`, the SDK): nothing runs, the same gating the mod uses today.

**Several sessions open:** each draws its own pane. Only a session whose Stats tab is shown runs
a `wb top`. Two shown at once run two: cheap enough, and simpler than sharing one.

## How it plays out

Four phases, each one release-sized change through `scripts/ship.sh`, each with its docs in the
same commit (CLAUDE.md, *Keeping the docs true*):

1. **Recording and `wb stats --json`.** The new row fields and `round_end`, `stats.json` and
   `wb stats set`, `wb stats --json`, and the text table's new sections. No UI change. It goes
   first so history starts accumulating before anything draws it.
   - Docs: *Every `wb` command*, *Files and formats* (the new keys and `stats.json`).
   - Tests: a `Stats` class in `tests/test_workbench.py`, and rows without the new fields still
     counted.
2. **The Stats tab, history half.** `TABS` gains `stats`. The `stats-1` atom, the history
   drawing, the Settings rows, and the Tasks tab's saving cut to one line with a way into Stats.
   - Docs: *Seeing what the agent does*, *The mod, inside*, *Couplings* (the new atom's tag).
   - Tests: `register.test.tsx`: the tab, a new install's empty state, `wb stats` called only
     while shown.
3. **`wb top` and the live half.** The `hardware.py` readers (busy %, VRAM without `lspci`, the
   NVIDIA stream), `wb top`, the `top-1` atom, and the Now section with the rack and queue.
   - Docs: the same sections, plus `wb top` in *Every `wb` command*.
   - Tests:
     - no spawn while hidden, and killed on leaving;
     - the sysfs readers against a fake `/sys` tree, as the `gpu_power_w` tests do;
     - the CPU budget measured once and recorded in the release notes.
4. **Later, if wanted.**
   - A stats card in the web chat. Its `/api/stream` already carries the savings, and could carry
     a `stats` section.
   - Polish of bests and milestones.
   - Rollups if `results.jsonl` grows past a few thousand rows.

## Open questions and risks

- **GPU busy % under WSL** without NVIDIA, and **VRAM used on Apple**: not available today. Those
  fields are left out on those machines.
- **Live tok/s on servers without a `live` object.** Ollama has none; llama-server, LM Studio and
  vLLM are not checked (llama-server's `/slots` and `/metrics` may carry it). Until each is
  probed and tested, they show the last finished call's speed, labelled.
- **Live first-token time** for a request still in flight: not checked. The first token is in
  the session export, which `wb top` must not run. So the line shows the last finished call's.
- **Several GPUs.** One line each, and the rack says which GPU a model is on when the server says.
  **Remote models**: no telemetry, said so.
- **`results.jsonl` grows forever.** It is read in full for `wb stats --json`. Fine for
  thousands of rows. Rollups (phase 4) if a user gets far past that.
- **Encouragement that stays honest.** Estimates are labelled, a broken streak isn't mentioned,
  and nothing is ranked against other users. `wb savings --share` remains the only way anything
  leaves the machine, and only when the user pastes it.
- **The `2 → 3` key change for Settings** (*Where it lives*): the owner's call.
