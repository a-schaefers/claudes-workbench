# Models: servers, setup, updates and the weekly check

Read from the /workbench skill when no local model can run, the user asks about model servers or
updating workbench, or the weekly model check is due.

**No local model yet?** If `wb models` lists none that can run, do the model check below first:
recommend models for this hardware and offer to set them up in OpenCode (ask before any download).
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
user's yes (`--remove` undoes it). Another machine adds a model beside this GPU's, never speed to one model. Ollama cloud models (`:cloud`) run off this machine: never register one without
the user's explicit yes. On WSL2, a server on Windows isn't reachable on 127.0.0.1 under the
default NAT networking: `wb setup`/`wb scout` say when one answers at the Windows host, or give
the two ways (mirrored networking, or 0.0.0.0 plus `wb remote`); don't propose a second server
inside WSL for it.

**Updating workbench.** The mod checks weekly and tells the user; `/workbench-update` is theirs.
Asked to update it yourself: `wb update` (it refuses while tasks run; tell the user to restart
Claude Code or run `/reload-plugins` after).

**The weekly model check.** Once a week the mod asks you (or the user runs `/workbench-models`)
to check the local models against this machine. `wb scout` gives the facts: GPU and VRAM, RAM,
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
vLLM `--max-model-len`, Ollama's `OLLAMA_CONTEXT_LENGTH`), then `wb model add … --context N`;
`wb model add` reads the window from the server (Ollama, llama-server, LM Studio once the model
is loaded, vLLM) and refuses when it can't say: then `--context N`, the window it was started
with; ask the user if you don't know it. `wb doctor` flags an entry whose
context differs from what the server serves, or that has none. Results first, then speed. Recommend briefly (keep / add / retire, with sizes and why), and
only with a yes: on Ollama `wb model add <provider/model> --pull` (background); on another server,
download it the server's own way (`lms get`, a GGUF from Hugging Face) and `wb model add`; `wb model sizes` for what it
should take, `wb model retire <id> [--delete]`. Installed but unregistered models may belong to other tools: ask before touching
them. When you set a model aside, `wb scout --watch "…"` records what would bring it back:
the change to look for, and the test it must pass. A model you add: offer an overnight
`wb bench run --model <id>` before its first real task.

**The audio and image engines** are in the check too, when `wb scout` lists any (speech, sound
effects, music, the listener; images). For each, check its model's licence today against the
licence text, not its tag or the `notes` written at setup: audio licences change (one model went
non-commercial mid-2026), and some are free only below a revenue line or need a credit. Then look
for a newer local model of that kind that fits this machine better (the runtime's releases too),
and recommend keep / add / retire the same way, with the download size and what runs on the
CPU. A kind the project uses with no engine, or speech with no listener (its takes go unchecked),
is worth a line. Ask before downloading or removing anything; keep each engine's `notes` (its
licence) true in audio.json or images.json. Finish with `wb scout --done --notes "…"`. Nothing worth changing: one line, and done.
