<!-- Fill every section from real output. Delete these comments. See CONTRIBUTING.md.
     This page is public: no local paths, hostnames, usernames or project names. -->

## What and why

<!-- What the change does, and the problem it fixes, in a few sentences. -->

Closes #

## Why it is for everybody

<!-- Who else hits this, on what hardware, OS and models. A preference stays a local patch
     (CONTRIBUTING.md, section 1). Nothing here names a model, a server or a machine. -->

## Tests run

<!-- Each command, and its last line as printed (e.g. "Ran 40 tests ... OK (skipped=3)").
     Name every skipped test and why it skipped: a skip is not a pass. -->

- [ ] `python3 -m unittest` (or the classes named here: …)
- [ ] the same on Python 3.11
- [ ] `claude plugin validate mod && claude plugin test mod` (the change touches `mod/`), or not needed
- [ ] `BUN_BE_BUN=1 opencode test opencode-plugin/test && BUN_BE_BUN=1 opencode check -p opencode-plugin` (the change touches `opencode-plugin/` or the mod's core it imports), or not needed
- [ ] `tests/e2e_chat.mjs` in WebKit and Chromium (the change touches `chat.py`, `webpush.py` or `chat_static/`), or not needed

Skipped:

## Docs

- [ ] Each name the diff adds, renames, removes or changes the meaning of is grepped in CLAUDE.md
      and ARCHITECTURE.md, and every hit fixed. The names:
- [ ] The ARCHITECTURE.md section that describes the changed behaviour, reread and true:
- [ ] `tests/test_docs.py` passes

## OpenCode invocation

- [ ] Not changed (the runner's argv, the event or export fields)
- [ ] Changed: `tests/fake_opencode.py` updated in the same commit, and one real run done (CLAUDE.md,
      *Checks before committing*) / not done, because:

## The rest

- [ ] Runs on Python 3.11; no new dependency; no model name in code or the skill
- Written by: <!-- the agent (and model) that wrote it, e.g. "Claude Code for its user", or "by hand"; wb patch upstream's Workbench-Driver: trailer names the one that prepared it -->
- [ ] Read by the user it was written for, who said yes to sending it (`wb patch send`'s dialog, or by hand)
- The local patch it replaces: <!-- its `wb patch list` line, or "none" -->
