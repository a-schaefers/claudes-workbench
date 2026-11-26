# Contributing

For people and the agents working for them. Most fixes come from a user's Claude that hit a bug
in workbench: it patches it locally first, then, when the fix is good for everybody, offers it
here. Read the whole page before your first change; it is short on purpose.

## 1. For everybody, or just for you?

Decide this first, and say which in the commit and the pull request.

- **For everybody**: a bug, a false result (a report, a count or a check that says something
  untrue), a crash, a doc that is wrong, or an improvement any user would want on any hardware,
  any OS workbench supports and any model. It never hard-codes a model, a server, a path or a
  machine: models come from OpenCode's config and the router, hardware is measured.
- **Just for you**: a preference, a local path, a default that suits your machine, a workflow
  taste. Keep it as a local patch (below). It is not worth less; it just doesn't belong upstream.

When in doubt, it is yours: keep it local and open an issue describing the problem, not the patch.

## 2. Local patches

A marketplace install (the README's quickstart) can't keep a change: Claude Code re-clones its copy
on every update, and `wb update` refuses while that copy has local changes (it saves them under
`$WB_HOME/rescued/<time>/` first). Move to a clone you own, once:

```sh
wb patch init            # a full clone at ~/.local/share/workbench/source (--dir, or WB_SOURCE)
```

It carries over any changes the marketplace copy had as the first patches, points Claude Code's
marketplace, `wb` and the `/workbench` skill at the new clone, and refreshes the plugin; then
`/reload-plugins` in Claude Code. A developer's clone needs no init: `git switch -c local --track origin/main` in it
(one made without `--track` gets that upstream from the next `wb update` or `wb patch init`).
Run from a developer's clone, `wb update` checks Claude Code's git marketplace copy too, if there is
one, and refuses the same way over its changes (saved first): `git am` takes them into your clone,
and `claude plugin marketplace add <your clone>` makes it the marketplace, which is never re-cloned.

A patch is a commit on branch `local`, with a scope trailer:

```sh
git -C <the clone> commit -a -m "Short subject" --trailer "Scope: me"        # or "Scope: everybody"
wb patch list            # each patch, oldest first, with its scope; uncommitted changes; pr/ branches
wb patch drop <commit>   # remove one (nothing changes if the others no longer apply without it)
wb patch check <commit>  # its own tests on plain upstream: has upstream fixed it another way?
```

`wb update` keeps them: it fetches upstream and rebases `local` onto it in a scratch worktree,
never in the live tree. A patch upstream already has is dropped and reported as `landed upstream,
dropped from local`, and one whose own pull request merged (`wb patch send` recorded it; `wb
update` asks GitHub) as `your pull request #<n> merged: dropped the local patch`, however it was
reworded or squashed. A patch that no longer applies changes nothing: `wb update` names it, and
you resolve it by hand or drop it, and so does an untracked file on a path the rebased patches
add. `wb patch check <commit>` applies only the patch's tests to plain upstream and runs the ones
it added, each by its directory's runner (`mod/`'s through claude, `opencode-plugin/`'s through
OpenCode's Bun, `kernel/`'s props): when they pass there (a skipped one is no pass), upstream likely fixed the same thing
another way, and you may drop it (it drops nothing itself). So give every patch a test: `wb patch list` marks one without
` (no test)`. The tip before each rebase is kept at `refs/workbench/before-update`, its reflog listing the
earlier ones. A change under `mod/` reaches Claude Code
only once committed, through `wb update` (it copies the clone's HEAD into the plugin cache), then
`/reload-plugins`.

`wb` runs from this clone, so an edit in it takes effect at once: change it while no task runs
(`wb status`), and commit only what passes its tests.

## 3. Offering it upstream

1. **Search first**: `gh issue list --repo a-schaefers/workbench --state all --search
   "<words from the error>"`. Comment on an existing issue rather than opening a second.
2. **Open an issue** with the matching form (bug, feature, docs), filled from real output: the
   commit, `wb doctor`, `wb patch list`. `gh issue create --repo a-schaefers/workbench
   --web` opens the chooser; an agent without a browser writes the form's fields, in its order,
   under its labels as `### ` headings, into `--body-file`.
3. **Prepare the branch**: `wb patch upstream <commit> --name <slug>` makes branch `pr/<slug>`
   from fresh upstream main in its own worktree (`$WB_HOME/upstream/<slug>`), with only that
   commit cherry-picked, and copies `.github/pull_request_template.md` to
   `$WB_HOME/upstream/<slug>.md`. It runs the patch's own tests there and writes each command and
   its last line into that file (`Tests run by wb: …`, or `not run` and why), adds a
   `Workbench-Driver:` trailer to the branch's commit (the tool and model that prepared it: Claude
   Code, OpenCode, an agent or a terminal), and prints the suites to run, the template's sections
   still unfilled, `no Scope trailer` when the commit has none, the one `wb patch send` line and
   gh's state.
4. **Test it there** (section 4), and fill in every section of the body file (keep wb's lines). Read it as the
   public text it becomes: take out local paths, hostnames and project names.
5. **Send it**:
   ```sh
   wb patch send <slug> --title "<the commit's subject>" --body-file <the body file it printed>
   ```
   It forks the repository (unless it is yours), pushes `pr/<slug>` to the fork and opens the
   pull request (`--draft` for a draft). It refuses, sending nothing, while the branch has
   uncommitted changes, or the body is empty or still the template; a commit whose trailer is
   `Scope: me` is named in the dialog that asks you. A failed step says what is done already; running
   it again goes on from there. The commit message's body carries `Closes #N`. Once it merges,
   the next `wb update` drops the local patch and removes `pr/<slug>`, its worktree and its body;
   closed unmerged, it keeps the patch and says so once.

**The consent dialog.** In Claude Code the workbench plugin holds every command that would send
something to this repository (an issue, a comment, a fork, a push, a pull request, `wb patch
send`) and asks you in a dialog, showing the command and, for `wb patch send`, the title and the
body's start; the GitHub plugin's tools that write here ask the same. "Send it" lets that one
command through; nothing is remembered. While you are away on the web chat it is refused, and
waits for you. Inside Claude Code `wb patch send` itself refuses a send the dialog didn't approve
(the plugin not loaded, say): then you run it in a terminal of your own. Working in your own clone
of this repository (its `origin` this repository) isn't gated: that is a developer at work, not a
user's Claude. Claude Code's own copy of the plugin (under `~/.claude/plugins/`) is no developer's
clone, and is gated.

**gh setup.** `wb patch send` sends through [gh](https://cli.github.com), GitHub's CLI. `wb
doctor`, `wb patch upstream` and a refused send say its state and the next step: the install
line for your system (GitHub's own instructions), then `gh auth login --hostname github.com
--git-protocol ssh` (or `https`, then `gh auth setup-git`, which edits your global git config).
A change to `.github/workflows/` over https needs `gh auth refresh -h github.com -s workflow`.
Claude never handles your token: you run the login yourself (`! gh auth login …` in Claude
Code). The GitHub plugin for Claude Code is optional and not needed for any of this.

**No GitHub account?** You don't need one: `wb patch zip` writes your patches on `local` (or the
commits you name) as a .zip in `~/Downloads` (else `$WB_HOME/patches/out/`): a `git format-patch`
file per commit and a README.txt naming the upstream commit they apply to. Email it to
adam.schaefers@icloud.com, or send it any way you like; wb sends nothing. The patch files name the
commit's author as your git config has it: edit their `From:` lines first if you'd rather not.

Never `scripts/ship.sh`: it is the owner's road to main, pushing to this repository and arming
auto-merge, which only the owner can.
With a developer's clone and no `wb patch`, the same steps by hand: fork, a branch from
`origin/main`, the commit, the tests, `gh pr create` with the filled-in template as its body.

## 4. The project's rules

From [CLAUDE.md](CLAUDE.md), which is the full version: read it before changing code, and imitate
the code around your change.

- **Standard library only**, no new dependency, no packaging. **Python 3.11** is the floor (CI
  runs 3.11; developers often run newer): run what you touch with a 3.11 too.
- **Never name a model** in code or the skill (test fixtures may). Nothing hard-coded about
  one machine.
- **Comments say why**, often with the incident and its date. **Test names read as sentences**
  (`test_a_patch_upstream_merged_is_dropped`); a regression test cites its issue.
- **The docs change in the same commit as the code** (CLAUDE.md, *Keeping the docs true*): for
  each name the diff adds, renames, removes or changes the meaning of, `grep` CLAUDE.md and
  ARCHITECTURE.md and fix every hit; reread the ARCHITECTURE.md section describing the behaviour
  you changed. A new command or flag goes in *Every `wb` command*, a key or file in *Files and
  formats*, a module in CLAUDE.md's *Layout*, an environment variable in its *State and config*,
  anything Claude does differently in `skill/workbench/SKILL.md`. README.md stays simple.
  `tests/test_docs.py` catches a missing command, flag, module or variable, not a stale sentence.
- **Tests**: `python3 -m unittest` (the whole suite, about 20 minutes; one class:
  `python3 -m unittest tests.test_workbench.Update`); for `mod/`: `claude plugin validate mod
  && claude plugin test mod`; for `opencode-plugin/`, or the mod's core it imports
  (`mod/hooks/core.ts`, `kernel.js`, `golden/`): `BUN_BE_BUN=1 opencode test opencode-plugin/test &&
  BUN_BE_BUN=1 opencode check -p opencode-plugin`. `wb patch upstream` names each one a patch
  needs, and CI runs both plugins' (its `mod` and `opencode-plugin` jobs), so a patch for one
  path can't break the other. **A skip is not a pass**: the real-OpenCode tests (`RealOpenCode*`)
  skip without `opencode` on PATH, the Landlock tests without Landlock (macOS, old kernels), and
  a few without git-lfs, ssh-keygen or ffmpeg. `python3 -m unittest -v 2>&1 | grep skipped` names
  them; a change to `context.py`, the config layer, `gitguard.py` or `lane.py` needs them run where
  they don't skip, or the pull request says they weren't.
- **How OpenCode is invoked or read** (the runner's argv, the event or export fields): update
  `tests/fake_opencode.py` in the same commit, and do one real run (CLAUDE.md, *Checks before
  committing*), or say in the pull request that you couldn't.
- **The web chat** (`chat.py`, `webpush.py`, `chat_static/`): also the browser test,
  `PLAYWRIGHT=…/node_modules/playwright node tests/e2e_chat.mts`, in WebKit and Chromium. Its
  scripts are TypeScript in `chat_static/src/`: edit them there and run `python3 scripts/web.py build`
  (it fetches the pinned TypeScript once, by hash; no npm or Node needed), then commit the `.ts` and the
  `.js` it writes together. Never edit a generated `.js`, and add no hand-written JavaScript anywhere.
- **The kernel, without its toolchain** (`kernel/`; its contract `kernel/BOUNDARY.md`): a
  TypeScript, Python or docs change needs no toolchain. The committed `mod/hooks/kernel.js` is what
  the mod loads, and `python3 -m unittest` (`tests/test_kernel.py`) checks it offline, each failure
  naming its fix. A change to a Python value the kernel's Tables copies (`store.ACTIVE`,
  `NEEDS_CLAUDE`, `PARKS`, `CLOSED`, `WORKING`, `STOPPED`, `OPEN_ORDER`, `ask_mcp.NO_ANSWER`, `chat.LISTENING_FOR`,
  `spend.WAKE_OPENING`) fails the suite until `python3 scripts/kernel.py build`, with
  `mod/hooks/golden/tables.ts` changed to match. A change to `kernel/src` needs the toolchain once:
  `build` fetches the pinned purs and esbuild by hash (network; Linux, WSL2 or macOS, x64 or arm64)
  into `~/.cache/workbench/kernel`; then `python3 scripts/kernel.py check`, whose props run on Bun
  (`bun`, or OpenCode's own). `wb patch upstream` names that check for a patch that touches
  `kernel/`. A `wb update` rebase that meets `kernel.js`, `kernel.d.ts` or `Tables.purs` stops:
  take either side, then `build`.

## 5. What happens next

- **CI waits for the owner.** A fork's workflows run only once the owner approves the run, every
  time, not only the first. Then `check` runs the suite in six shards on Python 3.11, the mod's
  tests and the kernel's check, with a read-only token and no secrets.
- **The owner reviews and merges.** main needs a pull request, `check` green and one approving
  review; only the owner's own pull requests are approved automatically. A push after an approval
  dismisses it, and the new run waits for approval again: push fixes together.
- **No version bumps.** The plugin has no version on purpose (installs track main's commits), and
  releases are cut by hand by the owner. Don't edit version strings or release notes.
- **Your local patch goes away by itself**: once the pull request merges, the next `wb update`
  finds upstream has it and drops it from `local`. If the owner changed it in review, the rebase
  may not recognise it: `wb patch list`, and `wb patch drop` it if it is still there.
