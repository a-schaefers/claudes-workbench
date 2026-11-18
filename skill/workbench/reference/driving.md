# Driving a task: permissions, directories, the web, the web chat

Read from the /workbench skill when an agent asks for a command or a directory, a spec needs web
research, or a `[web chat]` message arrives.

## Commands and directories an agent asks for
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

## The web is yours; the agent tests in a browser but does not browse
Research (for a spec, an
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

## The web chat
While workbench mode is on in a session, the user can reach you from their phone
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
