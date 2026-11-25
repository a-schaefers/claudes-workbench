# Models: servers, setup, updates and the weekly check

Read from the /workbench skill when no role can run, the user asks about model servers or
updating workbench, or the weekly model check is due.

**No role can run?** In this order:
- No GPU model, and a provider with a stored key (`wb setup`'s `ok  API keys: …`): the skill's
  one-question first run (two of that provider's models for the three roles, the cap, one
  `wb route … --day-usd`; an OpenRouter role in a route of its own, `--order` if they name backends).
- Neither: one question, as the skill's first run says: a local server (*A local server in one
  yes*, below), each option filled in for this machine, or `/connect` in OpenCode for an API
  provider; the user picks.

**A local server in one yes.** `wb scout` first (GPU, VRAM, RAM, disk, the servers on PATH and
answering). For each server that fits the OS and GPU, one line: the server, the model you picked
for this VRAM (weights plus a window of at least 32K inside VRAM, the model check's research
below, verified today), its download size, and what the yes runs. Each server is installed its
own official way, run as the user would run it, never wrapped in wb code (its URLs change, so they
live here, not in code). Ollama serves its own small default window unless its service sets
`OLLAMA_CONTEXT_LENGTH`, and `wb model add` refuses a window it can't read, so the install sets
one that lasts: `<N>` is the window you picked (32768 or more), `<K>` that over 1024.
- Ollama on macOS (no sudo): `brew install ollama && brew services start ollama`, or its app.
  Never `launchctl setenv OLLAMA_CONTEXT_LENGTH`: it is gone at the next logout or reboot, Ollama
  then serves its own default (4K under 24 GB) and the runner fails every task before a round
  (`short_window`). The window goes in the model instead, Ollama's own way for its OpenAI API: a
  `num_ctx`, which Ollama and `wb model add` both read before the environment (`wb optimize
  apply` makes its models so). In the background, `ollama pull <family>:<tag> && curl -fsS http://127.0.0.1:11434/api/create
  -d '{"model":"<family>:<tag>-<K>k","from":"<family>:<tag>","parameters":{"num_ctx":<N>},
  "stream":false}' && wb model add ollama/<family>:<tag>-<K>k` (no download twice: the new name
  shares the weights). A Mac whose tasks fail `short_window` after a reboot lost a
  `launchctl setenv`: make that model the same way and `wb model add` it.
- Ollama on Linux and WSL2 (WSL2 needs systemd on): its install.sh needs sudo, and your Bash has
  no terminal for the password, so the user runs this one line, with Claude Code's `!` prefix or in
  a terminal of their own if sudo can't ask there: `curl -fsSL https://ollama.com/install.sh | sh
  && sudo mkdir -p /etc/systemd/system/ollama.service.d && printf
  '[Service]\nEnvironment="OLLAMA_CONTEXT_LENGTH=<N>"\n' | sudo tee
  /etc/systemd/system/ollama.service.d/context.conf >/dev/null && sudo systemctl daemon-reload &&
  sudo systemctl restart ollama`. That is the only step that stays theirs.
- Then, on Ollama on Linux, `wb model add ollama/<model> --pull` in the background: it writes the
  provider, downloads, reads the served window, and starts `wb optimize` when due (no separate
  yes); on macOS the `wb model add` above does the same after Ollama's own pull.
- LM Studio: its app (`lms get` downloads), then `wb model add <p>/<m> --url … --context N`.
  llama-server or vLLM: their own packages, then `wb model add <p>/<m> --url … --context N
  --start "…" --stop "…"`, as below.
- Then `wb doctor`, then `wb try`.

**Work with the model server the user already runs**, whichever it is: `wb scout` lists the server
commands on PATH and the servers answering here (one that "wants an API key" needs it in its
provider's `options.apiKey`: ask the user for it). Never steer them to one they don't use. With
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
`local: true`, set in models.json before `wb model add --url` (which refuses an address off this
machine without it). Its server must listen on the LAN, firewalled to this machine (model servers
have no login); plain HTTP carries the code unencrypted (Tailscale encrypts; its address still
needs the flag). An Ollama there: `wb remote <host> [--context N]` does all of it, with the
user's yes (`--remove` undoes it). Another machine adds a model beside this GPU's, never speed to one model. Ollama cloud models (`:cloud`) run off this machine: they are API models,
never GPU candidates, run only through a role the user sets; never register one without
the user's explicit yes. On WSL2, a server on Windows isn't reachable on 127.0.0.1 under the
default NAT networking: `wb setup`/`wb scout` say when one answers at the Windows host, or give
the two ways (mirrored networking, or 0.0.0.0 plus `wb remote`); don't propose a second server
inside WSL for it.

**Several GPUs, one model per card** (on a first run with several cards, or when the user asks):
Claude sets it up on request; there is no UI for it. One server per card or card group, each
started with only its cards visible (`CUDA_VISIBLE_DEVICES=0`, HIP's `HIP_VISIBLE_DEVICES` on AMD),
on its own port; for Ollama, `OLLAMA_HOST=127.0.0.1:<port>` beside the mask. Register each model
on its server: `wb model add <provider>/<model> --url http://127.0.0.1:<port>/v1 --context N
--start "<the command with its mask and port>" --stop "<its stop>"`, a provider per server. A
role's GPU model (`wb route <role> --gpu <id>`, the user's) then picks its cards. Name no server
software as required: whichever the user runs, started that way, works.

**Updating workbench.** The mod checks weekly and tells the user; `/workbench-update` is theirs.
Asked to update it yourself: `wb update` (it refuses while tasks run; tell the user to restart
Claude Code or run `/reload-plugins` after). A refusal over local changes in the marketplace
clone, or a local patch that no longer applies: `reference/patching.md`.

**The weekly model check.** Once a week the mod toasts that it is due; `/workbench-models` asks you
to check the models for each role, GPU and API alike; the GPU side against this machine. `wb scout` gives the facts: GPU and VRAM, RAM,
disk, what is installed and registered, real-task results, and the watch list
(models set aside, with what each needs before it is tried again: check each against what is
new). Then research what is
newest and best: open-weight coding models with reliable tool calls, the sizes and quantizations
that fit the VRAM with room for the context, and runtime releases worth having. Use primary
sources (their APIs first; a blocked page is not checked: reference/driving.md, *The web is yours*) and its
reputation in recent reports, verify before you name anything. Fit the models to the machine `wb scout` describes, whatever
its size: a model that fits entirely in VRAM (or the GPU's share of unified memory) beats a bigger
one that spills into RAM, and on a big card the best is a larger model, a longer context, or both.
The window sets the slice of every task (`slice:` in the report), so where the VRAM has room past
the weights, raise the context the server serves (llama-server `-c`, LM Studio's context length,
vLLM `--max-model-len`, Ollama's `OLLAMA_CONTEXT_LENGTH`, on macOS a `num_ctx` model as
above), then `wb model add … --context N`;
`wb model add` reads the window from the server (Ollama, llama-server, LM Studio once the model
is loaded, vLLM) and refuses when it can't say: then `--context N`, the window it was started
with; ask the user if you don't know it. `wb doctor` flags an entry whose
context differs from what the server serves, or that has none. Results first, then speed. Recommend briefly (keep / add / retire, with sizes and why), and
only with a yes: on Ollama `wb model add <provider/model> --pull` (background); on another server,
download it the server's own way (`lms get`, a GGUF from Hugging Face) and `wb model add`; `wb model sizes` for what it
should take, `wb model retire <id> [--delete]`; a model now under another id (a provider or alias renamed, its old id retired): `wb model rename <old> <new>` keeps its record. Installed but unregistered models may belong to other tools: ask before touching
them. When you set a model aside, `wb scout --watch "…"` records what would bring it back:
the change to look for, and the test it must pass. A model you add: offer an overnight
`wb bench run --model <id>` before its first real task.

**API roles in the check.** For each role on API, `wb scout`'s API section gives its model's
record (tasks, accepted, paid a task, rate-limited rounds) and the week's spend beside the day cap
(`≈`: at the catalog's prices, an estimate; `wb model price` only if the user finds it off their bill),
and a count of each connected provider's models. Look up the same providers' newer or cheaper
models and any price change on the providers' own pages, and whether thinking would pay for
`significant` from its record. Recommend in chat only: changing a role is the user's, in the
dialog (on their words, `wb route`) or the pane's Settings tab. Name a free model only when the
user asks, with what it does with the code (some keep or train on it).

**The audio and image engines** are in the check too, when `wb scout` lists any (speech, sound
effects, music, the listener; images). For each, check its model's licence today against the
licence text, not its tag or the `notes` written at setup: audio licences change (one model went
non-commercial mid-2026), and some are free only below a revenue line or need a credit. Then look
for a newer local model of that kind that fits this machine better (the runtime's releases too),
and recommend keep / add / retire the same way, with the download size and what runs on the
CPU. A kind the project uses with no engine, or speech with no listener (its takes go unchecked),
is worth a line. Ask before downloading or removing anything; keep each engine's `notes` (its
licence) true in audio.json or images.json. Finish with `wb scout --done --notes "…"`. Nothing worth changing: one line, and done.
