"""Local image generation, art-directed by Claude.

Claude writes the prompt, `wb image` has a local image model make a few candidates, Claude looks
at each one and picks, refines the prompt, or regenerates from a seed it liked; the image it keeps
goes into the project, committed by Claude like any other change. Every image gets a sidecar
.json with its prompt, seed, size and engine, so it can be made again.

An engine is whatever runs an image model on this machine, described in
~/.config/workbench/images.json (`$WB_IMAGES`), name -> entry:
  {"command": "<argv template>"}   a CLI such as stable-diffusion.cpp; placeholders {prompt}
                                   {out} {width} {height} {seed}; `ref_arg` (e.g. "-r") is
                                   repeated before each --ref image
  {"url": "http://127.0.0.1:…/v1/images/generations", "model": "…"}
                                   any server with the OpenAI images API (SGLang, vLLM-Omni,
                                   LocalAI…); the response's b64_json is saved (or
                                   its url fetched); "key": its API key ({env:}, {file:})
plus optional "label", "notes" (licence, what it is good at: shown to Claude), "timeout" (s),
"gpu" (false: not this machine's GPU; a url on another host is off it unless true).
The first entry is the default. No model is named here: the user installs one and describes it.

A generation on the GPU holds the GPU lock and first unloads the coding models, as a task does;
while a task holds the GPU it refuses at once, unless asked to wait. A command engine exits when it's done, so
its memory is free again; a url engine is a server the user runs, which keeps its model in GPU
memory for as long as it is up: workbench can't stop it.
"""
from __future__ import annotations

import base64
import contextlib
import json
import os
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import media, models, store

PLACEHOLDERS = ("prompt", "out", "width", "height", "seed")
_settle = media.settle


def config_path() -> Path:
    return Path(os.environ.get("WB_IMAGES") or Path.home() / ".config/workbench/images.json")


def engines() -> dict[str, dict]:
    return media.load(config_path())


def ready(e: dict) -> str:
    return media.ready(e, base=config_path().parent)


def engine(name: str | None) -> tuple[str, dict]:
    have = engines()
    if not have:
        raise KeyError(f"no image engine: describe one in {config_path()}")
    if name is None:
        name = next(iter(have))
    if name not in have:
        raise KeyError(f"no image engine {name!r} (have: {', '.join(have)})")
    return name, have[name]


def _argv(e: dict, values: dict, refs: list[Path]) -> list[str]:
    argv = media.fill(e["command"], {k: values[k] for k in PLACEHOLDERS})
    for r in refs:
        argv += [e["ref_arg"], str(r)]
    return argv


def _command(e: dict, values: dict, refs: list[Path], log: Path) -> None:
    with open(log, "a") as f:
        r = media.run_group(_argv(e, values, refs), float(e.get("timeout", 1800)),
                            stdin=subprocess.DEVNULL, stdout=f, stderr=subprocess.STDOUT)
    if r.returncode or not Path(values["out"]).is_file():
        tail = "\n".join(log.read_text(errors="replace").strip().splitlines()[-8:])  # one bad byte: #743
        raise RuntimeError(f"the engine exited {r.returncode} without an image:\n{tail}")


def _server(e: dict, values: dict) -> None:
    """An OpenAI images server; its b64_json saved, or the url it gave fetched. A reply of another
    shape is a RuntimeError quoting it: `{"data": [{"url": …}]}` gave "wb: b64_json", and `{"data":
    []}` or a page that isn't JSON a traceback (#597)."""
    body = {"prompt": values["prompt"], "n": 1, "size": f"{values['width']}x{values['height']}",
            "response_format": "b64_json", "seed": values["seed"]}
    if e.get("model"):
        body["model"] = e["model"]
    timeout = float(e.get("timeout", 1800))
    raw, _ = media.post(e, e["url"], json.dumps(body).encode(), "application/json", timeout,
                        config_path().parent)
    try:
        item = json.loads(raw)["data"][0]
        b64, url = item.get("b64_json"), item.get("url")
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        raise RuntimeError(f"{e['url']} sent no image: {media._gist(raw) or '(an empty reply)'}") from None
    if isinstance(b64, str) and b64:
        try:
            Path(values["out"]).write_bytes(base64.b64decode(b64))
        except ValueError:
            raise RuntimeError(f"{e['url']} sent a b64_json that isn't base64") from None
    elif isinstance(url, str) and url.startswith(("http://", "https://")):
        # it ignored response_format: fetched, the key sent only to the engine's own server
        own = url.startswith(re.sub(r"/v1/.*$", "", e["url"]) + "/")
        req = urllib.request.Request(url, headers=media.auth(e, config_path().parent) if own else {})
        try:
            with models._open(req, timeout=timeout) as r:
                Path(values["out"]).write_bytes(r.read())
        except urllib.error.HTTPError as err:
            raise RuntimeError(f"{e['url']} ignored response_format b64_json and its image url "
                               f"answered HTTP {err.code}") from None
    else:
        raise RuntimeError(f"{e['url']} sent no image: {media._gist(raw)}")


def generate(prompt: str, out_dir: Path, n: int = 4, size: tuple[int, int] = (1024, 1024),
             seed: int | None = None, name: str | None = None, refs: list[Path] = (),
             say=print, wait: bool = False) -> list[Path]:
    """Make n images (seeds seed, seed+1, …); returns their paths, each with a .json sidecar.
    While anything holds the GPU it refuses at once (RuntimeError), unless ``wait``: queued
    behind a running task, it used to block silently past the Bash tool's limit, and Claude
    couldn't answer that task's question meanwhile (the 2026-11-16 audit)."""
    name, e = engine(name)
    if why := ready(e):
        raise RuntimeError(f"image engine {name}: {why}")
    refs = [Path(r).resolve() for r in refs]
    if refs and not (e.get("command") and e.get("ref_arg")):
        raise RuntimeError(f"image engine {name} takes no reference images (no ref_arg)")
    for r in refs:
        if not r.is_file():
            raise RuntimeError(f"no reference image {r}")
    out_dir.mkdir(parents=True, exist_ok=True)
    seed = int(time.time()) % 1_000_000 if seed is None else seed
    made = []
    # the GPU to itself, queued with the tasks; an engine off this GPU runs beside them (#587)
    hold = media.gpu("wb image", say, wait, media.serving(e)) if media.on_gpu(e) else contextlib.nullcontext()
    with hold:
        for i in range(n):
            s = seed + i
            # into a path of its own, then published under a name nothing has: two prompts with
            # the same first six words and seed wrote one PNG, the second over the first, and an
            # engine that wrote nothing passed on the old PNG, its sidecar rewritten (#267)
            with tempfile.TemporaryDirectory(prefix=store.scratch(".wb-image-"), dir=out_dir) as tmp:
                fresh = Path(tmp) / f"{media.slug(prompt, 'image')}-{s}.png"
                values = {"prompt": prompt, "out": fresh, "width": size[0], "height": size[1], "seed": s}
                t0 = time.time()
                if e.get("command"):
                    _command(e, values, refs, out_dir / "engine.log")
                else:
                    _server(e, values)
                secs = round(time.time() - t0, 1)
                if fresh.is_symlink() or not fresh.is_file() or not fresh.stat().st_size:
                    raise RuntimeError(f"image engine {name} wrote no image")
                store.write_json(fresh.with_suffix(".json"), {
                    "prompt": prompt, "seed": s, "width": size[0], "height": size[1],
                    "engine": name, "refs": [str(r) for r in refs], "secs": secs, "ts": store.now()})
                out = media.free(out_dir, f"{media.slug(prompt, 'image')}-{s}", ".png")
                media.publish(fresh, out)
            say(f"{out}  ({secs} s)")
            made.append(out)
    return made
