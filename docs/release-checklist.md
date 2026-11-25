# Release checklist

Every release, before `gh release create`. Copy this file to `docs/releases/<version>.md`, fill
it in on the release commit, commit it with the version bump, and link it from the release
notes. Mark each line **pass**, **skip** (with why) or **fail** (with its issue), and keep the
three apart: a skip is not a pass. A release may ship with a skip only when its notes say so.
(The 2026-11-19 audit: the platforms the README names were never all run before a release.)

## CI on the release commit

- [ ] `check` green (the required check: Ubuntu, dash as `/bin/sh`, Python 3.11, the pinned
      Claude Code and OpenCode).
- [ ] `nightly` green on the commit: `gh workflow run nightly.yml --ref main`, then
      `gh run watch`. Its three jobs:
  - [ ] `check`: the newest Claude Code, the runner's newer Python.
  - [ ] `macos`: Apple silicon, Python 3.11, OpenCode (no /proc, no inotify, no Landlock), its
        five shards each finished (not cancelled), every failure fixed or with an issue.
  - [ ] `wsl`: WSL2's Debian, dash, a user who isn't root, OpenCode.
- [ ] `opencode`: the newest OpenCode, or the break it found has an issue.
- [ ] `chat`, when the web chat changed since the last release.

## What the README offers

- [ ] Every feature and command the README offers runs at the tagged commit, not only on main: in
      a checkout of the tag, each one's `wb` command answers (`bin/wb <command> --help` exits 0),
      and an offer the tag can't keep is cut from the README on the release commit, or waits for
      the next release. (#422: 1.8's README offered audio; `wb audio` landed after the tag, #410,
      and at the tag `wb audio --help` exits 2.)

## By hand, on real machines

Best effort, never a gate: the project has neither the users nor the hardware nor the funding to
run every platform, server and surface by hand before a release, so other hardware is supported
from the source, its docs and CI, and from what users report (`wb doctor`'s output, the OS, the
GPU, the model server). Run what is at hand and mark the rest **skip** (#275, closed 2026-11-20).

Every result by hand, in this section and the next, is recorded against the exact candidate:
the SHA it ran (`git rev-parse --short HEAD` in the checkout used), the OS and the repo's
filesystem, the versions (Python, git, OpenCode, Claude Code, the kernel and its Landlock ABI as
`wb doctor` says it, and whatever the run is about: Docker, tailscale, Ollama), the hardware,
and the result: **pass**, **skip** with its reason, or **fail** with its issue. Where a table has
only a Notes column, the notes hold them. A run on an earlier commit stays recorded as that SHA,
and counts for the candidate only when `git diff <sha> <candidate>` touches nothing it
exercises, which its notes say. (#482, the audit of v1.8: what would earn 9/10.)

The whole road, as a `--trial` task on a scratch repo from `wb init`: install, sample (`wb try`),
delegate (`wb start`), answer a question (`wb answer`), review, interrupt (`wb steer --now` or
`wb stop`), resume (`wb feedback`), then `wb accept --merge`. Check the merged file and commit
yourself, not only what `wb` reports.

| Where | Result | Notes |
|---|---|---|
| Linux (Debian or Ubuntu) | | |
| macOS on Apple silicon, natively | | |
| WSL2, the repo in WSL's own file system | | |
| WSL2, the repo on a Windows drive (`/mnt/c`) | | |

On at least one of them:

- [ ] Python 3.11.
- [ ] git in another language (`LANGUAGE=fr`).
- [ ] A repo path with spaces.
- [ ] A laptop's sleep and wake during a round; and left idle with a task running, it stays
      awake (macOS, Linux: `systemd-inhibit --list` names workbench), or on WSL2 `wb start
      --backlog` says it can't.

### The release evidence

The runs the audit of v1.8 named (#482). Each row is one run, and a row naming several checks
passes only when all of them do: its notes name any one skipped or failed. Best effort like the
rest of this section: a skip is recorded with its reason, never a gate.

| Run | Result | SHA | OS, filesystem | Versions | Hardware | Notes (skip reason, fail issue) |
|---|---|---|---|---|---|---|
| `wb accept --merge` with a signing key that needs unlocking (a passphrase, a pinentry) | | | | | | |
| `wb accept --merge --no-sign`, headless (no terminal, no agent to unlock a key) | | | | | | |
| A sparse, blobless clone (`--filter=blob:none`, `git sparse-checkout`) of a large repo | | | | | | |
| A repo with Git LFS files | | | | | | |
| `wb bench run` with `wb events` running, on a kernel with an 8,192-watch limit (`fs.inotify.max_user_watches`) | | | | | | |
| A `--test` suite using Docker, under Landlock ABI 9 or newer | | | | | | |
| The web chat behind a real `tailscale serve --bg 1337`, on its https address, with the default chat.json (#448) | | | | | | |
| The web chat on an IPv6-only host | | | | | | |
| The web chat on real phones: a request backgrounded, the server interrupted between writes, an old prompt answered, reconnecting | | | | | | |
| A clean install on Debian or Ubuntu with Python 3.11 | | | | | | |
| An upgrade from a 1.7 state directory with open tasks and chat history | | | | | | |
| macOS on Apple silicon, natively: a signed accept, sleep during a task, no child left after a round | | | | | | |
| macOS on Apple silicon: a task on a Metal model | | | | | | |
| WSL2, the repo on the distro's filesystem: CRLF, exec bits, symlinks, submodules, `wb shot` | | | | | | |
| WSL2, the repo on a mounted Windows volume (`/mnt/c`): CRLF, exec bits, symlinks, submodules, `wb shot` | | | | | | |
| A real AMD GPU workload | | | | | | |
| A real NVIDIA GPU workload | | | | | | |
| A low-VRAM or mixed GPU host | | | | | | |
| A shared Ollama, with a long request from outside workbench during a task | | | | | | |

## Models and long runs

| Setup | Result | Notes |
|---|---|---|
| CPU only, or little VRAM | | |
| A discrete GPU | | |
| Metal | | |
| Unified memory on Linux (an APU, a DGX Spark) | | |
| Several GPUs, Ollama limited to some (`CUDA_VISIBLE_DEVICES`) | | |
| A remote Ollama (`wb remote`) | | |
| A server with an API key and several models (`wb model add --url`) | | |

- [ ] More tasks than fit, queued, each run in turn.
- [ ] A model load interrupted (`wb stop` while it loads), and the next task runs.
- [ ] The web chat in two tabs, with reconnects and a long history.
