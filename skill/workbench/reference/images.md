# Images: you art-direct, a local model draws

Read from the /workbench skill when the project needs an image (a sprite, an icon, a background).

When the project needs an image (a sprite, an icon, a background, a README picture), and
`wb image --list` shows a ready engine, make it locally instead of asking the user for one.
Agents can't make images; one that needs an asset asks you: answer first (a placeholder now, the
asset at review), and make it once the GPU is free.
1. **Brief it like an art director.** Say what's in it, then the style, composition, palette and
   lighting, the size the project needs, and what to avoid. Name the use ("a 64x64 icon on
   a flat background") so the framing fits.
2. `wb image "<prompt>" --n 4 --size WxH [--seed S] [--ref img]` makes candidates (seeds S, S+1,
   ...) and prints their paths. Run it with Bash `run_in_background: true` and read its
   notification: a batch can outrun Bash's 2 minutes. **Read every one**: look at it yourself, never pick by filename.
3. Keep one, or rewrite the prompt for what was wrong and go again. A seed you liked comes back
   with `--seed` and a changed prompt. Stop after three rounds and show the user the best.
4. Copy the keeper into the project (resized or cropped as needed), commit it, and say which
   engine, prompt and seed made it (its `.json` sidecar has them). A project often ignores
   `*.png` for its screenshots: `git check-ignore -v <file>`, and add an exception for the
   asset directory. For a sprite on a black background drawn additively, black is see-through
   and no alpha channel is needed.
Mind the engine's `notes` in `wb image --list`: an image model's licence can restrict where and
how its output may be used. An engine listed `(off the GPU)` (`"gpu": false` in images.json, or a
url on another host) runs beside tasks. One pinned to a card (`"devices"` or `"beside"` in
images.json) waits only for tasks on that card. Any other generation needs the GPU to itself: while a task holds it,
`wb image` refuses and names it; `--wait` (Bash `run_in_background: true`) queues behind it.
While it runs (or waits), `wb start` refuses a new task (exit 4) unless `--queue`, which waits
behind it; feedback, next and resume queue behind it. A url engine is a server that keeps its GPU
memory while it runs, unless its entry has an `unload` URL (`(no unload URL)` in the listing says
it hasn't): then ask the user to stop it before big coding tasks.

**No ready engine? Offer to set one up; never set it up unasked.** This is an optional step, as
ARCHITECTURE.md's Images section (in the workbench code, two directories above `readlink -f "$(command -v wb)"`) says. First read `wb scout`: it names an image server already answering on ComfyUI's (8188, or
ComfyUI Desktop's 8000) or AUTOMATIC1111's port, on WSL2 at the Windows host too, with the entry
that would use it, and ask whether the user runs one
elsewhere: AUTOMATIC1111, Forge or SD.Next (port 7860; an entry `{"url":
"http://127.0.0.1:7860/sdapi/v1/txt2img"}` speaks its API, `"unload":
".../sdapi/v1/unload-checkpoint"`), ComfyUI (port 8188, the Desktop app 8000; `{"url": "http://127.0.0.1:8188",
"workflow": "<file>"}`, a workflow the user saved with Export (API), `{prompt}` in its text node,
`{seed}`, `{width}`, `{height}` where it wants them; it unloads through `/free` by itself), or one
with the OpenAI images API. Otherwise use `wb scout`'s OS and GPU, then propose in one
message: a runtime that fits (stable-diffusion.cpp on Vulkan for AMD or Intel, CUDA for NVIDIA,
Metal on macOS; built from its upstream source, with packages from the OS's own repositories),
and an image model you've just checked. It must fit the VRAM without spilling, and its licence
must allow the user's use and country. Some licences exclude whole regions, so read the licence
text, not the tag. Give the download size. Wait for a yes, then install, write
`~/.config/workbench/images.json` (the licence goes in `notes`), make one test image, and look at
it before saying it works.
