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
                                   LocalAI…); the response's b64_json is saved
plus optional "label", "notes" (licence, what it is good at: shown to Claude), "timeout" (s).
The first entry is the default. No model is named here: the user installs one and describes it.

A generation holds the GPU lock and first unloads the coding models, as a task does.
"""
from __future__ import annotations

import base64
import json
import os
import re
import shlex
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import models, store

PLACEHOLDERS = ("prompt", "out", "width", "height", "seed")


def config_path() -> Path:
    return Path(os.environ.get("WB_IMAGES") or Path.home() / ".config/workbench/images.json")


def engines() -> dict[str, dict]:
    p = config_path()
    if not p.is_file():
        return {}
    data = json.loads(p.read_text())
    return {k: v for k, v in data.items() if isinstance(v, dict)}


def engine(name: str | None) -> tuple[str, dict]:
    have = engines()
    if not have:
        raise KeyError(f"no image engine: describe one in {config_path()}")
    if name is None:
        name = next(iter(have))
    if name not in have:
        raise KeyError(f"no image engine {name!r} (have: {', '.join(have)})")
    return name, have[name]


def ready(e: dict) -> str:
    """'' when the engine can run, else why not."""
    if e.get("command"):
        exe = os.path.expanduser(shlex.split(e["command"])[0])
        return "" if shutil.which(exe) or Path(exe).is_file() else f"{exe} not found"
    if e.get("url"):
        root = re.sub(r"/v1/.*$", "", e["url"])
        try:
            urllib.request.urlopen(root, timeout=2)
        except urllib.error.HTTPError:
            pass  # something answers
        except OSError:
            return f"nothing answers at {root}"
        return ""
    return "neither command nor url"


def _slug(prompt: str) -> str:
    return "-".join(re.findall(r"[a-z0-9]+", prompt.lower())[:6]) or "image"


def _argv(e: dict, values: dict, refs: list[Path]) -> list[str]:
    argv = []
    for tok in shlex.split(e["command"]):
        tok = os.path.expanduser(tok)  # no shell expands ~ for us; the prompt is filled in after
        for k in PLACEHOLDERS:
            tok = tok.replace("{" + k + "}", str(values[k]))
        argv.append(tok)
    for r in refs:
        argv += [e["ref_arg"], str(r)]
    return argv


def _command(e: dict, values: dict, refs: list[Path], log: Path) -> None:
    with open(log, "a") as f:
        r = subprocess.run(_argv(e, values, refs), stdin=subprocess.DEVNULL, stdout=f,
                           stderr=subprocess.STDOUT, timeout=int(e.get("timeout", 1800)))
    if r.returncode or not Path(values["out"]).is_file():
        tail = "\n".join(log.read_text().strip().splitlines()[-8:])
        raise RuntimeError(f"the engine exited {r.returncode} without an image:\n{tail}")


def _server(e: dict, values: dict) -> None:
    body = {"prompt": values["prompt"], "n": 1, "size": f"{values['width']}x{values['height']}",
            "response_format": "b64_json", "seed": values["seed"]}
    if e.get("model"):
        body["model"] = e["model"]
    req = urllib.request.Request(e["url"], json.dumps(body).encode(),
                                 {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=int(e.get("timeout", 1800))) as r:
        data = json.loads(r.read())
    Path(values["out"]).write_bytes(base64.b64decode(data["data"][0]["b64_json"]))


def generate(prompt: str, out_dir: Path, n: int = 4, size: tuple[int, int] = (1024, 1024),
             seed: int | None = None, name: str | None = None, refs: list[Path] = (),
             say=print) -> list[Path]:
    """Make n images (seeds seed, seed+1, …); returns their paths, each with a .json sidecar."""
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
    with store.locked(store.home() / "gpu.lock"):  # one model on the GPU at a time
        models.free_gpu(say=say)
        for i in range(n):
            s = seed + i
            out = out_dir / f"{_slug(prompt)}-{s}.png"
            values = {"prompt": prompt, "out": out, "width": size[0], "height": size[1], "seed": s}
            t0 = time.time()
            if e.get("command"):
                _command(e, values, refs, out_dir / "engine.log")
            else:
                _server(e, values)
            secs = round(time.time() - t0, 1)
            store.write_json(out.with_suffix(".json"), {
                "prompt": prompt, "seed": s, "width": size[0], "height": size[1],
                "engine": name, "refs": [str(r) for r in refs], "secs": secs, "ts": store.now()})
            say(f"{out}  ({secs} s)")
            made.append(out)
    return made
