# Audio: you direct, a local model performs, and you can't hear it

Read from the /workbench skill when the project needs a sound (a voice line, narration, a sound
effect, music).

When `wb audio --list` shows a ready engine, make the sound locally instead of asking the user
for one. Agents can't make audio. One that needs a sound asks you: answer first (silence or a
placeholder for now, the sound at review), and make it once the GPU is free (a `CPU` engine
needs no wait).

**You can't hear.** Every candidate comes with one line of checks: its length (and the length
asked for), peak and clipping, loudness, lead and tail silence, gaps, and for speech what the
listener heard and its word error rate (WER). With ffmpeg each also gets a spectrogram `.png`.
Never say a sound "sounds" good. Say what the checks show, and let the user's ears decide taste.

1. **Brief it like a director.** Speech: the exact script, with numbers, acronyms and odd names
   spelled out as they should be said ("twenty twenty-six", not "2026"). Short sentences. If the
   engine takes a described voice, describe it: age, accent, pace, mood. Sfx: the source, the
   space, the length, and whether it loops or needs a clean tail. Music: genre, tempo,
   instruments, mood, the length, vocals or instrumental, and where it plays (a menu loop,
   credits).
2. `wb audio "<script or description>" [--kind speech|sfx|music] --n 3 [--seconds S] [--voice V]
   [--lyrics FILE] [--seed S]`. **Read every check line and look at every spectrogram.** Never
   pick by filename.
3. **Speech:** keep a take with WER 0% (or only a harmless difference, such as a number written
   as digits), no clipping and no long gap. If none qualifies, rewrite the script (phonetic
   spellings for the words it fumbles, shorter sentences) and go again. For long text, make one
   call per few sentences and join the takes afterwards with ffmpeg: long generations drift.
4. **Sfx and music:** drop candidates that are "cut short", SILENT, clipped, or have gaps you
   didn't ask for, and ones whose spectrogram is wrong for the brief (all one band, a hard cut,
   a tail that never decays on a loop). Tell the user which you'd keep and why, and ask them to
   listen (give the paths). Stop after three rounds and show the best.
5. Convert it to what the project uses with ffmpeg (format, trim, `loudnorm` to the project's
   level, a loop point), commit it, and say which engine, text and seed made it (the `.json`
   sidecar has them). Credit the model when its licence requires it (CC-BY).

Mind each engine's `notes` in `wb audio --list`: an audio model's licence can forbid commercial
use or require attribution. A `--ref` clip clones a voice: use only the user's own voice or one
they have the rights to, and never a real person's voice taken from a recording found online. A
GPU engine needs the GPU to itself: while a task holds it, `wb audio` refuses and names the
holder, and `--wait` (Bash `run_in_background: true`) queues behind it. A url engine without an
`unload` URL keeps its GPU memory while it runs: ask the user to stop it before big coding tasks.

**No ready engine? Offer to set one up; never set it up unasked.** This step is optional, as
ARCHITECTURE.md's Audio section says (in the workbench code, two directories above
`readlink -f "$(command -v wb)"`). Read `wb scout` for the OS, GPU and RAM, then propose in one
message:
- the runtime that section recommends, on the right backend (Vulkan for AMD or Intel, CUDA for
  NVIDIA, Metal on macOS, the CPU without a GPU), built from its upstream source or its release
  binary;
- the smallest set of models this project needs (often one speech model and a listener), each
  checked today against its licence text, not its tag. Some are non-commercial, some are free
  only below a revenue line, and one changed its licence mid-2026;
- the download size, and what runs on the CPU.

Wait for a yes. Then install, write `~/.config/workbench/audio.json` (the licence goes in
`notes`, `"gpu": false` for CPU engines, the listener as `"kind": "listen"`), make one test
clip, and check its line (a speech test with WER 0%) before saying it works.
