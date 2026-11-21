"""What `wb image` and `wb audio` share: engines described in a JSON file, a command line filled
in without a shell, the GPU to themselves, and candidates published under names nothing has.

An engine file maps a name to an entry: {"command": "<argv template>"} or {"url": "…"}, plus
whatever the kind of media adds. The first entry is the default; non-object values are ignored.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import shlex
import shutil
import time
import urllib.error
from pathlib import Path

from . import hardware, models


def load(path: Path) -> dict[str, dict]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text())
    except ValueError as e:
        raise RuntimeError(f"{path} doesn't parse ({e}); fix it by hand") from None
    return {k: v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}


def ready(e: dict) -> str:
    """'' when the engine can run, else why not."""
    if e.get("command"):
        exe = os.path.expanduser(shlex.split(e["command"])[0])
        return "" if shutil.which(exe) or Path(exe).is_file() else f"{exe} not found"
    if e.get("url"):
        root = re.sub(r"/v1/.*$", "", e["url"])
        try:
            models._open(root, timeout=2).close()  # no proxy to this machine (models._open)
        except urllib.error.HTTPError:
            pass  # something answers
        except OSError:
            return f"nothing answers at {root}"
        return ""
    return "neither command nor url"


def fill(command: str, values: dict) -> list[str]:
    """The command's argv with each `{name}` of ``values`` filled in, token by token, never
    through a shell. One pass: a filled-in value is never scanned again, so a prompt that says
    "{seed}" reaches the engine verbatim (the 2026-11-16 audit: it got the seed and the out path)."""
    pat = re.compile(r"\{(" + "|".join(map(re.escape, values)) + r")\}")
    argv = []
    for tok in shlex.split(command):
        tok = os.path.expanduser(tok)  # no shell expands ~ for us; the values are filled in after
        argv.append(pat.sub(lambda m: str(values[m.group(1)]), tok))
    return argv


def slug(text: str, fallback: str) -> str:
    return "-".join(re.findall(r"[a-z0-9]+", text.lower())[:6]) or fallback


def settle(say, timeout: float = 90, keep=()) -> None:
    """Wait until the unloaded models have let go of the VRAM. free_gpu's unload returns before
    the memory does: seen 2026-11-16, an engine starting a second later found 1.1 GB free of 16.
    Only what free_gpu(keep) lets go of is waited for (models.freed): a server it can't stop, or
    another host's model, used to cost every generation the whole timeout (the 2026-11-16 audit),
    and so did a model it keeps loaded (#467)."""
    if models._fake():
        return
    deadline = time.time() + timeout
    while any(models.freed(m, keep) for m in models.resident()) and time.time() < deadline:
        time.sleep(1)
    last = None
    while time.time() < deadline:  # then until the GPU's used VRAM stops falling
        used = sum(g["vram_used"] for g in hardware.gpus(windows=False) if g.get("vram_used") is not None)
        if last is not None and last - used < 64 * 2 ** 20:
            return
        last = used
        time.sleep(1)
    say(f"the GPU's memory was still being released after {timeout:.0f} s; generating anyway")


@contextlib.contextmanager
def gpu(who: str, say, wait: bool):
    """The GPU to itself, queued with the tasks (`models.GpuLease` with no model), the coding
    models unloaded and their memory released. While anything holds it, it refuses at once unless
    ``wait``: queued behind a running task, `wb image` used to block silently past the Bash tool's
    limit, and Claude couldn't answer that task's question meanwhile (the 2026-11-16 audit)."""
    with models.GpuLease(None, who, say, wait=wait):
        models.free_gpu(say=say)
        settle(say)
        yield


def free(out_dir: Path, stem: str, suffix: str, extra: tuple[str, ...] = ()) -> Path:
    """`<stem><suffix>`, or `<stem>-2<suffix>` and on, whichever has neither the file nor its
    .json sidecar (nor any ``extra`` file beside it): an earlier candidate is never replaced (#267)."""
    for k in range(1, 10_000):
        p = out_dir / f"{stem}{'' if k == 1 else f'-{k}'}{suffix}"
        if not any(os.path.lexists(p.with_suffix(s)) for s in (suffix, ".json", *extra)):
            return p
    raise RuntimeError(f"{out_dir} holds 9,999 files named {stem}: pick another directory")


def publish(fresh: Path, out: Path, extra: tuple[str, ...] = ()) -> None:
    """Move a candidate made in its own temporary directory, its sidecar (and any ``extra``
    suffixes beside it, such as a spectrogram) to ``out``'s name: the sidecar first, so a file
    is never seen without its record."""
    os.replace(fresh.with_suffix(".json"), out.with_suffix(".json"))
    for suf in extra:
        if fresh.with_suffix(suf).is_file():
            os.replace(fresh.with_suffix(suf), out.with_suffix(suf))
    os.replace(fresh, out)
