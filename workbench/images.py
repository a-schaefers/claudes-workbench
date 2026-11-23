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
                                   LocalAI…), or with "api": "a1111" (a /sdapi/ url says so)
                                   AUTOMATIC1111's, Forge's or SD.Next's txt2img ("steps"
                                   optional), or with "api": "comfyui" (a "workflow" says
                                   so) ComfyUI's: "workflow", a workflow saved in API format
                                   with {prompt} {seed} {width} {height} in it; the
                                   response's b64_json is saved (or
                                   its url fetched); "key": its API key ({env:}, {file:});
                                   "unload": a URL that frees its GPU memory, "unload_body"
plus optional "label", "notes" (licence, what it is good at: shown to Claude), "timeout" (s),
"gpu" (false: not this machine's GPU; a url on another host is off it unless true).
The first entry is the default, unless "default": "<engine name>" names another (#1047: the pane's
Settings row, `wb image --default NAME`). No model is named here: the user installs one and describes it.
What an engine writes must be a whole PNG, JPEG or WebP; a JPEG or WebP keeps its own suffix.

A generation on the GPU holds the GPU lock and first unloads the coding models, as a task does;
while a task holds the GPU it refuses at once, unless asked to wait. A command engine exits when it's done, so
its memory is free again; a url engine is a server the user runs, which keeps its model in GPU
memory for as long as it is up, unless its "unload" URL (POSTed "unload_body", else {}, after
each batch) makes it let go.
"""
from __future__ import annotations

import base64
import http.client
import json
import os
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import media, models, store

PLACEHOLDERS = ("prompt", "out", "width", "height", "seed")
_settle = media.settle


def config_path() -> Path:
    return Path(os.environ.get("WB_IMAGES") or Path.home() / ".config/workbench/images.json")


def engines() -> dict[str, dict]:
    return {k: _defaults(v) for k, v in media.load(config_path()).items()}


def _defaults(e: dict) -> dict:
    """A ComfyUI engine unloads through its own /free unless its entry says otherwise (`"unload":
    ""` for none): ComfyUI keeps every model it loaded in GPU memory until asked (#753, #749)."""
    if e.get("command") or api(e) != "comfyui" or "unload" in e or not isinstance(e.get("url"), str):
        return e
    return {**e, "unload": comfy_root(e["url"]) + "/free",
            "unload_body": e.get("unload_body", {"unload_models": True, "free_memory": True})}


def ready(e: dict) -> str:
    if e.get("api") not in (None, *APIS):
        return f"not usable: api must be one of {', '.join(APIS)}"
    if "steps" in e and (isinstance(e["steps"], bool) or not isinstance(e["steps"], int) or e["steps"] < 1):
        return "not usable: steps must be a whole number"
    if e.get("url") and not e.get("command") and api(e) == "comfyui":
        try:
            workflow(e)
        except RuntimeError as err:
            return f"not usable: {err}"
    return media.ready(e, base=config_path().parent)


def engine(name: str | None) -> tuple[str, dict]:
    have = engines()
    if not have:
        raise KeyError(f"no image engine: describe one in {config_path()}")
    if name is None:
        name = default_name(have)
    if name not in have:
        raise KeyError(f"no image engine {name!r} (have: {', '.join(have)})")
    return name, have[name]


def default_name(have: dict[str, dict]) -> str | None:
    """The engine `wb image` runs without --engine: the one images.json's "default" names, else
    (no key, or a name that isn't an engine) the first entry, as before #1047."""
    named = media.named_default(config_path(), "default")
    return named if named in have else next(iter(have), None)


def set_default(name: str) -> str:
    """`wb image --default NAME` (#1047): images.json's "default", the line saying so."""
    have = engines()
    if name not in have:
        raise KeyError(f"no image engine {name!r} (have: {', '.join(have)})" if have
                       else f"no image engine: describe one in {config_path()}")
    media.set_default(config_path(), "default", name)
    return f"default image engine: {name}"


def choices() -> dict:
    """The pane's Images row (#1047, `wb models --json`'s media.image): the default and each
    engine, with whether it needs this GPU (media.on_gpu)."""
    have = engines()
    return {"default": default_name(have),
            "engines": [{"name": k, "gpu": media.on_gpu(e)} for k, e in have.items()]}


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


def kind(path: Path) -> tuple[str, int, int] | None:
    """The image a file holds, as (format, width, height) with format "png", "jpeg" or "webp",
    or None when its first bytes say it isn't one: an engine or server that wrote an HTML error
    page, or a JPEG, was published as a .png and reported as an image (#744, the image side of
    #592). A file that starts like an image but isn't whole raises ValueError saying what is
    missing: a bare PNG signature, a JPEG cut before its end marker or a WebP shorter than its
    RIFF size passed the magic bytes, was published, and its sidecar gave the size asked for,
    not one measured (#946). Trailing bytes after a whole image pass, as decoders take them."""
    data = Path(path).read_bytes()
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ("png", *_png(data))
    if data.startswith(b"\xff\xd8\xff"):
        return ("jpeg", *_jpeg(data))
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ("webp", *_webp(data))
    return None


def _png(data: bytes) -> tuple[int, int]:
    """Its IHDR size, after walking its chunks to IEND, each length within the file."""
    if data[12:16] != b"IHDR" or len(data) < 33:
        raise ValueError("no IHDR chunk after the PNG signature")
    w, h = int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if not (w and h):
        raise ValueError(f"a {w}x{h} PNG")
    at = 8
    while at + 12 <= len(data):
        n, typ = int.from_bytes(data[at:at + 4], "big"), data[at + 4:at + 8]
        if at + 12 + n > len(data):
            break                                       # cut off inside this chunk
        if typ == b"IEND":
            return w, h
        at += 12 + n
    raise ValueError("a PNG cut off before its IEND chunk")


def _jpeg(data: bytes) -> tuple[int, int]:
    """Its frame (SOFn) size, found by walking the segments to the scan, and an end marker after
    the scan: entropy-coded data stuffs every 0xFF, so FFD9 after the first SOS is the EOI (an
    EXIF thumbnail's own EOI comes before it)."""
    at, size = 2, None
    while at + 4 <= len(data):
        if data[at] != 0xFF:
            raise ValueError("a JPEG whose segments don't line up")
        marker = data[at + 1]
        if marker == 0xFF:                              # fill bytes before a marker
            at += 1
            continue
        if marker in (0x01, *range(0xD0, 0xD8)):        # markers without a length
            at += 2
            continue
        n = int.from_bytes(data[at + 2:at + 4], "big")
        if n < 2 or at + 2 + n > len(data):
            break
        # SOF0-15 less DHT (C4), JPG (C8) and DAC (CC)
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC) and n >= 7:
            size = (int.from_bytes(data[at + 7:at + 9], "big"), int.from_bytes(data[at + 5:at + 7], "big"))
        if marker == 0xDA:                              # SOS: the image data follows
            if not size:
                raise ValueError("a JPEG with no frame (SOF) before its scan")
            if not (size[0] and size[1]):
                raise ValueError(f"a {size[0]}x{size[1]} JPEG")
            if data.find(b"\xff\xd9", at + 2 + n) < 0:
                raise ValueError("a JPEG cut off before its end marker (FFD9)")
            return size
        at += 2 + n
    raise ValueError("a JPEG cut off before its image data")


def _webp(data: bytes) -> tuple[int, int]:
    """Its size from the first chunk (VP8X canvas, VP8 lossy frame, VP8L lossless header), with
    the file at least as long as its RIFF size says and that chunk within it."""
    riff = int.from_bytes(data[4:8], "little")
    if len(data) < riff + 8:
        raise ValueError(f"a WebP of {len(data)} bytes whose RIFF size says {riff + 8}")
    typ, n, body = data[12:16], int.from_bytes(data[16:20], "little"), data[20:]
    if len(data) < 20 or 20 + n > riff + 8 or len(body) < n:
        raise ValueError("a WebP whose first chunk doesn't fit it")
    if typ == b"VP8X" and n >= 10:
        w, h = 1 + int.from_bytes(body[4:7], "little"), 1 + int.from_bytes(body[7:10], "little")
    elif typ == b"VP8 " and n >= 10 and body[3:6] == b"\x9d\x01\x2a":
        w, h = int.from_bytes(body[6:8], "little") & 0x3FFF, int.from_bytes(body[8:10], "little") & 0x3FFF
    elif typ == b"VP8L" and n >= 5 and body[0] == 0x2F:
        bits = int.from_bytes(body[1:5], "little")
        w, h = 1 + (bits & 0x3FFF), 1 + ((bits >> 14) & 0x3FFF)
    else:
        raise ValueError(f"a WebP with no VP8/VP8L/VP8X image chunk (first: {typ!r})")
    if not (w and h):
        raise ValueError(f"a {w}x{h} WebP")
    return w, h


SUFFIX = {"png": ".png", "jpeg": ".jpg", "webp": ".webp"}


APIS = ("openai", "a1111", "comfyui")


def api(e: dict) -> str:
    """The API a url engine speaks: its `api`, else ComfyUI's for an entry with a `workflow`,
    A1111's for a `/sdapi/` url (AUTOMATIC1111, Forge, SD.Next), else OpenAI's. Only OpenAI's was
    spoken, so a Forge url got an OpenAI body and answered "sent no image", and ComfyUI refused
    it outright (#753)."""
    if e.get("api"):
        return e["api"]
    if e.get("workflow"):
        return "comfyui"
    return "a1111" if "/sdapi/" in str(e.get("url") or "") else "openai"


def comfy_root(url: str) -> str:
    """ComfyUI's server from its url: the root, or its /prompt."""
    url = url.rstrip("/")
    return url[:-len("/prompt")] if url.endswith("/prompt") else url


COMFY_FIELDS = ("prompt", "seed", "width", "height")
COMFY_POLL = 0.5  # s between /history checks


def workflow(e: dict):
    """The entry's ComfyUI workflow (`workflow`: a path, `~` expanded, relative to the engine
    file's directory), parsed and checked; a RuntimeError says what is wrong with it. The UI's
    own format (Save, not Export (API)) has "nodes" and "links", which /prompt refuses."""
    if not isinstance(e.get("workflow"), str) or not e["workflow"]:
        raise RuntimeError("a comfyui engine needs `workflow`: a workflow saved with ComfyUI's Export (API)")
    path = config_path().parent / os.path.expanduser(e["workflow"])
    try:
        graph = json.loads(path.read_text())
    except OSError as err:
        raise RuntimeError(f"can't read the workflow {path} ({err.strerror or err})")
    except ValueError as err:
        raise RuntimeError(f"the workflow {path} doesn't parse ({err})")
    if not (isinstance(graph, dict) and graph
            and all(isinstance(n, dict) and "class_type" in n for n in graph.values())):
        raise RuntimeError(f"the workflow {path} isn't in ComfyUI's API format: save it with Export (API)")
    if "{prompt}" not in json.dumps(graph):
        raise RuntimeError(f"the workflow {path} has no {{prompt}} in it: put it in the prompt node's text")
    return graph


def _fill(node, values: dict):
    """The workflow with {prompt} {seed} {width} {height} filled in: a string that is only
    {seed}, {width} or {height} becomes the number (ComfyUI's KSampler refuses "3"), any other
    gets each placeholder as text, in one pass (a prompt that says "{seed}" stays as written)."""
    if isinstance(node, dict):
        return {k: _fill(v, values) for k, v in node.items()}
    if isinstance(node, list):
        return [_fill(v, values) for v in node]
    if not isinstance(node, str):
        return node
    if (m := re.fullmatch(r"\{(seed|width|height)\}", node)):
        return int(values[m.group(1)])
    return re.sub(r"\{(" + "|".join(COMFY_FIELDS) + r")\}", lambda m: str(values[m.group(1)]), node)


def _get(e: dict, url: str, timeout: float) -> bytes:
    req = urllib.request.Request(url, headers=media.auth(e, config_path().parent))
    try:
        with models._open(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as err:
        raise RuntimeError(f"{url} answered HTTP {err.code}") from None
    except http.client.HTTPException as err:  # cut off mid-body (#925)
        raise media.cut_off(url, err) from None


def _comfyui(e: dict, values: dict) -> None:
    """ComfyUI's API: POST the filled workflow to /prompt, poll /history/<id> until it is done,
    then GET /view for its first output image (a SaveImage's before a PreviewImage's). Stopped
    before it finished (the timeout, a failed poll, wb ended), the prompt is dropped from its queue
    and interrupted: it went on drawing on the GPU after the lease was gone."""
    root, base, timeout = comfy_root(e["url"]), config_path().parent, float(e.get("timeout", 1800))
    graph = _fill(workflow(e), values)
    raw, _ = media.post(e, f"{root}/prompt", json.dumps({"prompt": graph}).encode(),
                        "application/json", timeout, base)
    try:
        pid = json.loads(raw)["prompt_id"]
    except (ValueError, KeyError, TypeError):
        pid = None
    if not isinstance(pid, str) or not pid:
        raise RuntimeError(f"{root}/prompt sent no prompt_id: {media._gist(raw) or '(an empty reply)'}") from None
    deadline = time.time() + timeout
    try:
        while True:
            try:
                item = json.loads(_get(e, f"{root}/history/{urllib.parse.quote(pid)}", 30)).get(pid)
            except (ValueError, AttributeError):
                item = None
            if isinstance(item, dict):
                break
            if time.time() > deadline:
                raise RuntimeError(f"ComfyUI at {root} hadn't finished the workflow after {timeout:.0f} s")
            time.sleep(COMFY_POLL)
    except BaseException:
        for path, body in (("/queue", {"delete": [pid]}), ("/interrupt", {"prompt_id": pid})):
            try:
                media.post(e, root + path, json.dumps(body).encode(), "application/json", 5, base)
            except Exception:  # best effort: the server may be gone
                pass
        raise
    status = item.get("status") if isinstance(item.get("status"), dict) else {}
    if status.get("status_str") == "error":
        said = next((m[1].get("exception_message") for m in status.get("messages") or []
                     if isinstance(m, list) and len(m) > 1 and m[0] == "execution_error"
                     and isinstance(m[1], dict)), None)
        raise RuntimeError(f"ComfyUI failed the workflow: {' '.join(str(said or 'no reason given').split())[:300]}")
    outputs = item.get("outputs") if isinstance(item.get("outputs"), dict) else {}
    made = [im for node in outputs.values() if isinstance(node, dict)
            for im in (node.get("images") if isinstance(node.get("images"), list) else [])
            if isinstance(im, dict) and isinstance(im.get("filename"), str)]
    if not made:
        raise RuntimeError("ComfyUI's workflow made no image: it needs a SaveImage node")
    im = next((x for x in made if x.get("type") == "output"), made[0])
    query = urllib.parse.urlencode({"filename": im["filename"], "subfolder": str(im.get("subfolder") or ""),
                                    "type": str(im.get("type") or "output")})
    Path(values["out"]).write_bytes(_get(e, f"{root}/view?{query}", timeout))


def _a1111(e: dict, values: dict) -> None:
    """AUTOMATIC1111's API (Forge and SD.Next speak it): POST {prompt, width, height, seed,
    steps?} to /sdapi/v1/txt2img, and images[0] is the image, base64."""
    body = {"prompt": values["prompt"], "width": values["width"], "height": values["height"],
            "seed": values["seed"], "batch_size": 1, "n_iter": 1}
    if e.get("steps"):
        body["steps"] = e["steps"]
    raw, _ = media.post(e, e["url"], json.dumps(body).encode(), "application/json",
                        float(e.get("timeout", 1800)), config_path().parent)
    try:
        b64 = json.loads(raw)["images"][0]
        Path(values["out"]).write_bytes(base64.b64decode(b64.split(",", 1)[-1]))
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        raise RuntimeError(f"{e['url']} sent no image: {media._gist(raw) or '(an empty reply)'}") from None


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
        except http.client.HTTPException as err:  # cut off mid-body (#925)
            raise media.cut_off(url, err) from None
    else:
        raise RuntimeError(f"{e['url']} sent no image: {media._gist(raw)}")


# The image servers' default ports, for `wb scout`: (port, the api that speaks to it, what probes
# it). 8000 is ComfyUI Desktop's (the packaged app for macOS and Windows: #923)
PORTS = ((8188, "comfyui", "/system_stats"), (8000, "comfyui", "/system_stats"),
         (7860, "a1111", "/sdapi/v1/options"))
# A port other servers use too (vLLM's is 8000): a hit there only when the reply has these keys,
# and any other answer is no image server, not "something answers" (#923)
SHARED_PORTS = {8000: ("system", "devices")}


def _probe(root: str, port: int, kind: str, probe: str) -> dict | None:
    """What answers at ``root``: {"root", "api"}, the api "" when something answers but not that
    API; None when nothing does, or something else does on a shared port."""
    keys = SHARED_PORTS.get(port)
    try:
        with models._open(root + probe, timeout=1) as r:
            body = json.loads(r.read())
        if isinstance(body, dict) and all(k in body for k in keys or ()):
            return {"root": root, "api": kind}
    except (urllib.error.HTTPError, ValueError):  # something answers, but not that API
        pass
    except (OSError, http.client.HTTPException):
        return None
    return None if keys else {"root": root, "api": ""}


def servers_here() -> list[dict]:
    """The image servers answering on their default ports on this machine, not through a proxy,
    1 s each: {"root", "api"}, the api "" when something answers there but not that API (an
    AUTOMATIC1111 started without --api). The skill proposed an 11 GB install to a user whose
    ComfyUI or Forge was already running (#753). On WSL2, when none answers here, the same ports
    at the Windows host too, all at once, with "windows": its address: in the default NAT
    networking a server on Windows isn't on 127.0.0.1, and the install was proposed again (#923;
    models.servers_here does the same, #663). None under WB_FAKE_MODELS (the tests)."""
    if models._fake():
        return []
    found = [x for port, kind, probe in PORTS
             if (x := _probe(f"http://127.0.0.1:{port}", port, kind, probe))]
    if (win := models.windows_host()) and not found:  # mirrored networking found them above
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(len(PORTS)) as pool:  # a firewall that drops: one wait, not three
            got = list(pool.map(lambda p: _probe(f"http://{win}:{p[0]}", *p), PORTS))
        found += [{**x, "windows": win} for x in got if x]
    return found


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
    # absolute on every Python: 3.11's mkdtemp kept a relative dir relative, so an engine that
    # changed directory wrote its {out} elsewhere and the run failed (3.12+ passed) (#751)
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    seed = int(time.time()) % 1_000_000 if seed is None else seed
    made = []
    # the GPU to itself, queued with the tasks; an engine off this GPU runs beside them (#587),
    # unless it is on a remote box the tasks use: then a turn there (#750)
    hold = media.turns("wb image", say, wait, e)
    # the unload inside the lease, failed or not: the next task loads beside nothing (#749)
    with hold, media.unloading([e], say, "image", config_path().parent):
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
                    {"a1111": _a1111, "comfyui": _comfyui}.get(api(e), _server)(e, values)
                secs = round(time.time() - t0, 1)
                if fresh.is_symlink() or not fresh.is_file() or not fresh.stat().st_size:
                    raise RuntimeError(f"image engine {name} wrote no image")
                try:
                    got = kind(fresh)
                except ValueError as why:  # nothing published, no sidecar (#946)
                    raise RuntimeError(f"image engine {name} wrote an incomplete image ({why})") from None
                if not got:
                    raise RuntimeError(f"image engine {name} wrote no image ({media.starts(fresh)})")
                fmt, width, height = got
                if SUFFIX[fmt] != fresh.suffix:  # published under its real suffix (#744)
                    fresh = fresh.rename(fresh.with_suffix(SUFFIX[fmt]))
                store.write_json(fresh.with_suffix(".json"), {
                    "prompt": prompt, "seed": s, "width": width, "height": height,  # measured (#946)
                    "engine": name, "refs": [str(r) for r in refs], "secs": secs, "ts": store.now(),
                    "format": fmt})
                out = media.free(out_dir, f"{media.slug(prompt, 'image')}-{s}", fresh.suffix)
                media.publish(fresh, out)
            say(f"{out}  ({secs} s)")
            made.append(out)
    return made
