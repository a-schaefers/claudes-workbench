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
   branch `local` there (`git -C <root> switch -c local --track origin/main`; one made without
   `--track` gets it from `wb update` or `wb patch init`).
2. Write the fix yourself, in the clone, imitating the code around it and following its CLAUDE.md
   (docs in the same commit, a test that fails without the fix: it is how `wb patch check` later
   tells whether upstream fixed the same thing). Don't delegate it: a task's
   runner, git shim and lane run from this very clone. `wb` runs from it too, so edit while no
   task runs (`wb status`) and run the tests you touched before committing.
3. Commit on `local` with its scope: `git -C <root> commit -a -m "<subject>" --trailer "Scope: me"`
   (or `"Scope: everybody"`). A change under `mod/` reaches Claude Code through `wb update` (it
   copies the committed HEAD into the plugin cache), then `/reload-plugins`.
4. `wb patch list` shows each patch, its scope, uncommitted changes and prepared `pr/` branches;
   a row ending ` (no test)` has no test of its own: write one before it goes upstream.
   `wb patch drop <commit>` removes one.

`wb update` keeps the patches: it rebases `local` onto upstream in a scratch worktree, reports a
patch upstream already has as `landed upstream, dropped from local: <subject>`, and moves the
live branch only when all apply. A pull request `wb patch send` opened is asked of GitHub first:
once merged, however upstream reworded or squashed it, the patch goes by itself (`your pull
request #<n> merged: dropped the local patch <subject>`); nothing to do. A patch that no longer
applies changes nothing: it names the patch and `wb patch check <sha>`; run that first. An
untracked file on a path the rebased patches add refuses too, before any task is stopped: move
it, then `wb update` again. The previous tip stays at `refs/workbench/before-update` (its
reflog: the earlier ones).

### Has upstream fixed it another way?
On `your pull request #<n> was closed without merging`, or a patch that no longer applies, run
`wb patch check <sha>`: it applies only the patch's tests to plain upstream, in a scratch tree,
and runs only the tests it added. It drops nothing.
- `upstream already passes this patch's tests (…)`: upstream likely fixed the same thing. Tell the
  user, and only on their yes run the `wb patch drop <sha>` it printed, then `wb update`.
- `upstream fails this patch's tests (…): keep it`: upstream still lacks it. Offer to resolve the
  conflict by hand (below) or, after a closed pull request, ask the user what they want.
- `… tests didn't all run here (…), so wb patch check can't tell …`: a test was skipped here (no
  Landlock, say), placed in no class, or the mod's tests need claude. A skip is not a pass: never
  offer the drop on it. Run them where they can run (`wb doctor` says what is missing), or
  compare by hand as below.
- `… has no test of its own …` or `… tests don't apply to upstream any more: compare by hand`:
  compare the patch with upstream's code yourself, then tell the user what you found; offer to
  write the missing test.
`couldn't ask GitHub about pull request #<n> (…): kept the local patch` is harmless: the next
`wb update` asks again.

Resolving a conflict by hand, never in the live tree (mid-conflict every `wb` call fails):
`git -C <root> worktree add --detach <scratch> local`, rebase there onto `local@{u}` and resolve,
`git -C <root> reset --keep <its HEAD>`, then remove the worktree.

## Upstream ("want me to PR it upstream for everybody?")
Only for a patch that is for everybody, and only on the user's yes. The mod holds every send to
workbench upstream (an issue, a comment, a fork, a push, a pull request) until the user approves
that one command in a dialog; a yes is never remembered. A pull request goes only through `wb
patch send`, which refuses under Claude Code what the dialog didn't approve, so never send one
with gh, git or a GitHub MCP tool yourself (those ask the same dialog). Show them what goes public
first, so the dialog only confirms what they have read. A "Don't send" is a no: tell them it
wasn't sent and leave it, unless they bring it up. If they are away on the web chat it is refused without a
dialog: tell them there what it would send; it waits until they are back at the terminal.
1. Search the issues (`gh issue list --repo <origin's owner/repo> --state all --search "<the error's
   words>"`); on a match, comment there instead. Otherwise draft the issue from the matching form
   (`.github/ISSUE_TEMPLATE/`): real output pasted, the commit, `wb doctor`, `wb patch list`.
   Show it to the user before filing it, and tick "filed by an agent" only once they have read it.
   Without a browser: `gh issue create --title … --label … --body-file <f>`, the form's fields in
   its order under its labels as `### ` headings.
2. A patch with no test gets one first (a commit on `local`, with the fix's): upstream asks for it,
   and `wb patch check` needs it. `wb patch upstream` says `it has no test: add one before
   sending` when it lacks one.
   `wb patch upstream <commit> --name <slug>`: branch `pr/<slug>` from fresh upstream, in its own
   worktree under `$WB_HOME/upstream/`, the commit cherry-picked. On a conflict, resolve it there,
   then `git cherry-pick --continue`. It prints the tests, the body file
   (`$WB_HOME/upstream/<slug>.md`, a copy of `.github/pull_request_template.md`), the one `wb patch
   send` line and gh's state, and runs none of them.
3. In that worktree, the tests it printed, on Python 3.11 too, each skip named (a skip is not a
   pass). Fill every section of the body file honestly (what you ran, what you didn't). Keep it
   outside the worktree, where it is: a file left there is a change `wb patch send` refuses.
4. Show the user the title and the whole body, word for word, and offer to redact what they may
   not want public: local paths, hostnames, project names, usernames. Then run the printed
   `wb patch send <slug> --title "…" --body-file <f>` (`--draft` if they want one); the mod's dialog
   shows the title and the body's start, and the user approves it there. Run it as a command of
   its own, the flags spelled out (`--title`, `--body-file`, never shortened, never a `$VAR`): the
   dialog refuses what it can't show. If wb says the dialog approved none (the mod isn't loaded),
   the user runs that command in a terminal of their own. `wb patch send` forks (unless the user
   owns upstream), pushes to their fork and opens the pull request; a failed step says what is
   done, and running it again goes on from there (a pull request already open is reported as
   opened). A `Scope: me` commit is refused unless
   `--anyway`: it is a sign the patch is theirs, not everybody's. Never `scripts/ship.sh`: it is
   the owner's.
5. Tell the user what happens next: CI waits for the owner to approve the run, the owner reviews
   and merges, and the next `wb update` drops the local patch by itself, however the owner
   reworded or squashed it, and its `pr/<slug>` branch, worktree and body file. If it is closed
   instead, `wb update` says so once and keeps the patch: then *Has upstream fixed it another
   way?* above.

## Without GitHub ("want a .zip of the patch to email upstream yourself?")
Many users have no GitHub account and don't want one. Whenever a patch is for everybody and the
user says no to GitHub (no gh, no account, a "don't want to", or they decline the install or the
login), offer the zip instead: "want a .zip of the patch to email upstream yourself?". `wb patch
send` refusing over a missing gh or login says so too. On a yes: `wb patch zip` (every patch on
`local` upstream lacks) or `wb patch zip <commit>…` (just those), `--name <slug>` for the file's
name. It is local: no fetch, no gh, nothing sent, so no dialog asks; it needs no `wb patch upstream`
first. Tell the user the path it printed (`~/Downloads`, else `$WB_HOME/patches/out/`) and the
address it printed, and pass on its one line that the patch files name the commit author (their
git name and email): they may edit those first. Sending it is theirs; never email it yourself.

## gh, GitHub's CLI
`wb patch upstream`, `wb patch send` and `wb doctor` say gh's state (`gh: ready as <login> over
<protocol>`, else the state and what to do).
- **Missing**: wb prints the one install line for this machine. Ask before installing it; a line
  wb marks `the user runs` (sudo, root, pamac) is theirs to run, as your Bash can't answer a
  password prompt.
- **Logged out or expired**: the login is the user's own: `! gh auth login --hostname github.com
  --git-protocol <ssh|https>` as wb prints it (a browser or a one-time code). Never ask for, read
  or print a token. Over https, `! gh auth setup-git` too, which edits their global git config:
  ask first.
- **A change under `.github/workflows/` over https** needs the token's workflow scope: `! gh auth
  refresh -h github.com -s workflow`.
- **The GitHub plugin for Claude Code is optional**, never needed for a pull request. If the user
  wants it: `claude plugin install github@claude-plugins-official`; it needs a personal access
  token the user creates on GitHub and sets as `GITHUB_PERSONAL_ACCESS_TOKEN` in their own shell
  profile (never from `gh auth token` unless they ask for exactly that), then a restart of Claude
  Code, and `claude mcp list` to check it connects. Its tools that write to upstream ask the
  same dialog; a pull request still goes through `wb patch send`.

## When `wb update` refuses over the marketplace copy
It found local changes in Claude Code's clone, which Claude Code's update would wipe (it
re-clones), and saved them under the `rescued/` path it printed. Tell the user, and offer
`wb patch init`: it carries the changes over. Run from a developer's clone, the refusal names
that clone instead: `git am` takes the saved patches into it, and `claude plugin marketplace add
<that clone>` makes it the marketplace (#1326). Never work around the refusal with Claude Code's
own `claude plugin marketplace update`.
