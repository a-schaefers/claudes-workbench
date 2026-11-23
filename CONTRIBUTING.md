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
`/reload-plugins` in Claude Code. A developer's clone needs no init: `git switch -c local` in it.

A patch is a commit on branch `local`, with a scope trailer:

```sh
git -C <the clone> commit -a -m "Short subject" --trailer "Scope: me"        # or "Scope: everybody"
wb patch list            # each patch, oldest first, with its scope; uncommitted changes; pr/ branches
wb patch drop <commit>   # remove one (nothing changes if the others no longer apply without it)
```

`wb update` keeps them: it fetches upstream and rebases `local` onto it in a scratch worktree,
never in the live tree. A patch upstream already has is dropped and reported as `landed upstream,
dropped from local`. A patch that no longer applies changes nothing: `wb update` names it, and
you resolve it by hand or drop it, and so does an untracked file on a path the rebased patches
add. The tip before each rebase is kept at `refs/workbench/before-update`, its reflog listing the
earlier ones. A change under `mod/` reaches Claude Code
only once committed, through `wb update` (it copies the clone's HEAD into the plugin cache), then
`/reload-plugins`.

`wb` runs from this clone, so an edit in it takes effect at once: change it while no task runs
(`wb status`), and commit only what passes its tests.

## 3. Offering it upstream

1. **Search first**: `gh issue list --repo a-schaefers/claudes-workbench --state all --search
   "<words from the error>"`. Comment on an existing issue rather than opening a second.
2. **Open an issue** with the matching form (bug, feature, docs), filled from real output: the
   commit, `wb doctor`, `wb patch list`. `gh issue create --repo a-schaefers/claudes-workbench
   --web` opens the chooser; an agent without a browser writes the form's fields, in its order,
   under its labels as `### ` headings, into `--body-file`.
3. **Prepare the branch**: `wb patch upstream <commit> --name <slug>` makes branch `pr/<slug>`
   from fresh upstream main in its own worktree (`$WB_HOME/upstream/<slug>`), with only that
   commit cherry-picked. It prints the next steps and runs none of them.
4. **Test it there** (section 4), then, each outward step only on the user's yes:
   ```sh
   gh repo fork a-schaefers/claudes-workbench --remote --remote-name fork    # in that worktree
   git push -u fork pr/<slug>
   gh pr create --repo a-schaefers/claudes-workbench --head <your login>:pr/<slug> --body-file <filled template>
   ```
   The body is `.github/pull_request_template.md`, filled in. The commit message's body carries
   `Closes #N`.

Never `scripts/ship.sh`: it is the owner's road to main, pushing to this repository and arming
auto-merge, which only the owner can.
With a developer's clone and no `wb patch`, the same steps by hand: fork, a branch from
`origin/main`, the commit, the tests, the pull request.

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
  `python3 -m unittest tests.test_workbench.Update`), and for `mod/`: `claude plugin validate mod
  && claude plugin test mod`. **A skip is not a pass**: the real-OpenCode tests (`RealOpenCode*`)
  skip without `opencode` on PATH, the Landlock tests without Landlock (macOS, old kernels), and
  a few without git-lfs, ssh-keygen or ffmpeg. `python3 -m unittest -v 2>&1 | grep skipped` names
  them; a change to `context.py`, the config layer, `gitguard.py` or `lane.py` needs them run where
  they don't skip, or the pull request says they weren't.
- **How OpenCode is invoked or read** (the runner's argv, the event or export fields): update
  `tests/fake_opencode.py` in the same commit, and do one real run (CLAUDE.md, *Checks before
  committing*), or say in the pull request that you couldn't.
- **The web chat** (`chat.py`, `webpush.py`, `chat_static/`): also the browser test,
  `PLAYWRIGHT=…/node_modules/playwright node tests/e2e_chat.mjs`, in WebKit and Chromium.

## 5. What happens next

- **CI waits for the owner.** A fork's workflows run only once the owner approves the run, every
  time, not only the first. Then `check` runs the suite in six shards on Python 3.11 and the mod's
  tests, with a read-only token and no secrets.
- **The owner reviews and merges.** main needs a pull request, `check` green and one approving
  review; only the owner's own pull requests are approved automatically. A push after an approval
  dismisses it, and the new run waits for approval again: push fixes together.
- **No version bumps.** The plugin has no version on purpose (installs track main's commits), and
  releases are cut by hand by the owner. Don't edit version strings or release notes.
- **Your local patch goes away by itself**: once the pull request merges, the next `wb update`
  finds upstream has it and drops it from `local`. If the owner changed it in review, the rebase
  may not recognise it: `wb patch list`, and `wb patch drop` it if it is still there.
