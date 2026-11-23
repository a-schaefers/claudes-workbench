# Patching workbench: a local patch, then upstream

Read from the /workbench skill when workbench itself misbehaves (a crash, a false report, a doc
that is wrong), when `wb update` refuses over local changes or a patch that no longer applies,
or when the user wants to change how workbench works for them. The contributor's rules are in
CONTRIBUTING.md at the root of workbench's code (two directories above `readlink -f "$(command
-v wb)"`); read it, and that root's CLAUDE.md, before the first patch.

## Everybody, or just this user?
Decide before you offer anything, and tell the user which and why in a line.
- **Everybody**: a bug, a crash, a false result (a count, a check or a report that says something
  untrue), a wrong doc, or an improvement any user would want on any hardware, OS and model.
  It hard-codes no model, server, path or machine.
- **Just them**: a preference, a local path, a default fit for this machine, a workflow taste.
  A local patch, never a pull request. When unsure, it is theirs: patch it locally and, if the
  problem is general, open an issue describing the problem, not the patch.

## A local patch ("want me to patch it?")
1. Once, on a marketplace install: `wb patch init` (on the user's yes: it switches Claude Code's
   marketplace to a full clone it makes, `~/.local/share/workbench/source` or `--dir`/`WB_SOURCE`,
   repoints `wb` and this skill, and refreshes the plugin). It refuses while tasks run. Changes
   the old copy had (or `wb update` rescued under `$WB_HOME/rescued/`) become the first patches;
   its stashes and other branches' commits are saved there only (it says so).
   Then the user runs `/reload-plugins`. On a clone the user already owns, it says so: work on
   branch `local` there (`git -C <root> switch -c local`).
2. Write the fix yourself, in the clone, imitating the code around it and following its CLAUDE.md
   (docs in the same commit, a test that fails without the fix). Don't delegate it: a task's
   runner, git shim and lane run from this very clone. `wb` runs from it too, so edit while no
   task runs (`wb status`) and run the tests you touched before committing.
3. Commit on `local` with its scope: `git -C <root> commit -a -m "<subject>" --trailer "Scope: me"`
   (or `"Scope: everybody"`). A change under `mod/` reaches Claude Code through `wb update` (it
   copies the committed HEAD into the plugin cache), then `/reload-plugins`.
4. `wb patch list` shows each patch, its scope, uncommitted changes and prepared `pr/` branches;
   `wb patch drop <commit>` removes one.

`wb update` keeps the patches: it rebases `local` onto upstream in a scratch worktree, reports a
patch upstream already has as `landed upstream, dropped from local: <subject>`, and moves the
live branch only when all apply. A patch that no longer applies changes nothing: it names the
patch; offer to resolve it by hand or to `wb patch drop` it. By hand, never in the live tree
(mid-conflict every `wb` call fails): `git -C <root> worktree add --detach <scratch> local`, rebase
there onto `local@{u}` and resolve, `git -C <root> reset --keep <its HEAD>`, then remove the
worktree. The previous tip stays at `refs/workbench/before-update` (its reflog: the earlier ones).
An untracked file on a path the rebased patches add refuses too, before any task is stopped:
move it, then `wb update` again.

## Upstream ("want me to PR it upstream for everybody?")
Only for a patch that is for everybody, and only on the user's yes; each outward step below
(the issue, the fork, the push, the pull request) is its own yes.
1. Search the issues (`gh issue list --repo <origin's owner/repo> --state all --search "<the error's
   words>"`); on a match, comment there instead. Otherwise draft the issue from the matching form
   (`.github/ISSUE_TEMPLATE/`): real output pasted, the commit, `wb doctor`, `wb patch list`.
   Show it to the user before filing it, and tick "filed by an agent" only once they have read it.
   Without a browser: `gh issue create --title … --label … --body-file <f>`, the form's fields in
   its order under its labels as `### ` headings.
2. `wb patch upstream <commit> --name <slug>`: branch `pr/<slug>` from fresh upstream, in its own
   worktree under `$WB_HOME/upstream/`, the commit cherry-picked. On a conflict, resolve it there,
   then `git cherry-pick --continue`. It prints the steps that follow and runs none.
3. In that worktree, the tests it printed, on Python 3.11 too, each skip named (a skip is not a
   pass). Then the fork, the push and `gh pr create` as printed, the body from
   `.github/pull_request_template.md` filled in honestly (what you ran, what you didn't).
   Never `scripts/ship.sh`: it is the owner's.
4. Tell the user what happens next: CI waits for the owner to approve the run, the owner reviews
   and merges, and the next `wb update` drops the local patch by itself. If the owner reworked it,
   `wb patch list` may still show it: `wb patch drop` it then.

## When `wb update` refuses over the marketplace copy
It found local changes in Claude Code's clone, which Claude Code's update would wipe (it
re-clones), and saved them under the `rescued/` path it printed. Tell the user, and offer
`wb patch init`: it carries the changes over. Never work around the refusal with Claude Code's
own `claude plugin marketplace update`.
