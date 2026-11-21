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
import signal
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from . import models


def run_group(argv: list[str], timeout: float, **kw) -> subprocess.CompletedProcess:
    """subprocess.run, with ``argv`` in a session of its own whose whole group is killed when it
    is done, on a timeout, or when wb is ended (cli._ends_cleanly makes SIGTERM a SystemExit).
    subprocess.run killed only its direct child: an engine that is a wrapper script left its
    worker running, holding the GPU, after the lease was gone (#589, the audit of 1.90).
    TimeoutExpired past ``timeout``, as subprocess.run."""
    p = subprocess.Popen(argv, start_new_session=True, **kw)
    try:
        out, err = p.communicate(timeout=timeout)
    except BaseException:
        _kill_group(p)
        p.wait()
        raise
    _kill_group(p)  # what it left running behind it
    return subprocess.CompletedProcess(argv, p.returncode, out, err)


def _kill_group(p: subprocess.Popen) -> None:
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def load(path: Path) -> dict[str, dict]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text())
    except ValueError as e:
        raise RuntimeError(f"{path} doesn't parse ({e}); fix it by hand") from None
    return {k: v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}


STRINGS = ("command", "url", "ref_arg", "model", "unload", "label", "notes", "key")


def problem(e: dict, kinds: tuple[str, ...] = ()) -> str:
    """What makes a hand-written entry unusable, or ''. Checked before anything runs: a quote left
    open in a command, "timeout": "10m" or "voices": "af_heart" used to end `wb scout` and the
    listings in a traceback, crash a generation after the lease was taken and the models unloaded,
    or be misread (the voice "a", a substring test; #596)."""
    for k in STRINGS:
        if k in e and not isinstance(e[k], str):
            return f"{k} must be a string"
    if e.get("command"):
        try:
            if not shlex.split(e["command"]):
                return "command is blank"
        except ValueError as err:
            return f"command doesn't split ({err})"
    for k in ("timeout", "seconds"):
        if k in e and (isinstance(e[k], bool) or not isinstance(e[k], (int, float)) or e[k] <= 0):
            return f"{k} must be a number of seconds"
    if "voices" in e and not (isinstance(e["voices"], list)
                              and all(isinstance(v, str) and v for v in e["voices"])):
        return "voices must be a list of names"
    if "gpu" in e and not isinstance(e["gpu"], bool):
        return "gpu must be true or false"
    if kinds and "kind" in e and e["kind"] not in kinds:
        return f"kind must be one of {', '.join(kinds)}"
    return ""


def auth(e: dict, base: Path | None = None) -> dict:
    """The url engine's `key` (OpenCode's `{env:NAME}` and `{file:path}` work, a relative path
    from the engine file's directory, ``base``) as a Bearer header: a server started with an API
    key failed 401 on every generation (#597)."""
    key = models._expand(e.get("key") or "", agent=False, base=base or Path.cwd())
    return {"Authorization": f"Bearer {key}"} if key else {}


def _gist(raw: bytes) -> str:
    text = " ".join(raw.decode(errors="replace").split())
    return text[:300] + ("…" if len(text) > 300 else "")


def post(e: dict, url: str, data: bytes, ctype: str, timeout: float,
         base: Path | None = None) -> tuple[bytes, str]:
    """POST to a url engine; its reply and Content-Type. An error status is a RuntimeError with
    the server's own words: "HTTP Error 400: Bad Request" hid "voice not found" (#597)."""
    req = urllib.request.Request(url, data, {"Content-Type": ctype, **auth(e, base)})
    try:
        with models._open(req, timeout=timeout) as r:
            return r.read(), r.headers.get("Content-Type") or ""
    except urllib.error.HTTPError as err:
        try:
            said = _gist(err.read())
        except OSError:
            said = ""
        raise RuntimeError(f"{url} answered HTTP {err.code}" + (f": {said}" if said else "")
                           + (" (it wants an API key: `key` in the engine's entry)"
                              if err.code in (401, 403) and not e.get("key") else "")) from None


def ready(e: dict, kinds: tuple[str, ...] = (), base: Path | None = None) -> str:
    """'' when the engine can run, else why not."""
    if why := problem(e, kinds):
        return f"not usable: {why}"
    if e.get("command"):
        try:
            exe = os.path.expanduser(shlex.split(e["command"])[0])
        except (ValueError, IndexError) as err:  # problem() says so first; a backstop
            return f"not usable: command doesn't split ({err})"
        return "" if shutil.which(exe) or Path(exe).is_file() else f"{exe} not found"
    if e.get("url"):
        root = re.sub(r"/v1/.*$", "", e["url"])
        try:  # no proxy to this machine (models._open)
            models._open(urllib.request.Request(root, headers=auth(e, base)), timeout=2).close()
        except urllib.error.HTTPError as err:  # something answers
            if err.code in (401, 403):  # it was listed as ready, then failed every generation (#597)
                return (f"{root} refused the key (HTTP {err.code})" if e.get("key")
                        else f"{root} wants an API key (HTTP {err.code}): `key` in its entry")
        except OSError:
            return f"nothing answers at {root}"
        return ""
    return "neither command nor url"


def _text(v) -> str:
    """A placeholder's value: a whole float without its ".0" (`--seconds 30` reached the engine
    as "30.0" where the entry's default gave "30": #596)."""
    return str(int(v)) if isinstance(v, float) and v.is_integer() else str(v)


def fill(command: str, values: dict) -> list[str]:
    """The command's argv with each `{name}` of ``values`` filled in, token by token, never
    through a shell. One pass: a filled-in value is never scanned again, so a prompt that says
    "{seed}" reaches the engine verbatim (the 2026-11-16 audit: it got the seed and the out path)."""
    pat = re.compile(r"\{(" + "|".join(map(re.escape, values)) + r")\}")
    argv = []
    for tok in shlex.split(command):
        tok = os.path.expanduser(tok)  # no shell expands ~ for us; the values are filled in after
        argv.append(pat.sub(lambda m: _text(values[m.group(1)]), tok))
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
    if not models.released(max(deadline - time.time(), 0)):  # then until the used VRAM stops falling
        say(f"the GPU's memory was still being released after {timeout:.0f} s; generating anyway")


def on_gpu(e: dict) -> bool:
    """Whether the engine needs this machine's GPU: not one that says "gpu": false (a CPU build),
    nor, unless it says "gpu": true, a url engine on another host. A remote or CPU image engine
    took the exclusive lease, unloaded the coding models and refused while any task ran (#587)."""
    if "gpu" in e:
        return e["gpu"] is not False
    if isinstance(e.get("url"), str) and not e.get("command"):
        return models._this_host(urlparse(e["url"]).hostname)
    return True


def serving(*engines: dict) -> list[str]:
    """The discovered models on the server a url engine is (one LocalAI or vLLM serving both
    coding and images): free_gpu keeps them, or it stopped the server just before the POST (#587)."""
    roots = {models.server_key(re.sub(r"/v1/.*$", "", e["url"]))
             for e in engines if isinstance(e.get("url"), str) and not e.get("command")}
    if not roots:
        return []
    return [m.id for m in models.discover().values() if m.server in roots]


@contextlib.contextmanager
def gpu(who: str, say, wait: bool, keep=()):
    """The GPU to itself, queued with the tasks (`models.GpuLease` with no model), the coding
    models unloaded (except ``keep``: the engine's own server) and their memory released. While
    anything holds it, it refuses at once unless ``wait``: queued behind a running task, `wb image`
    used to block silently past the Bash tool's limit, and Claude couldn't answer that task's
    question meanwhile (the 2026-11-16 audit)."""
    with models.GpuLease(None, who, say, wait=wait):
        models.free_gpu(keep=keep, say=say)
        settle(say, keep=keep)
        yield


def free(out_dir: Path, stem: str, suffix: str, extra: tuple[str, ...] = ()) -> Path:
    """`<stem><suffix>`, or `<stem>-2<suffix>` and on, whichever has neither the file nor its
    .json sidecar (nor any ``extra`` file beside it), claimed by creating its sidecar empty
    (O_EXCL): an earlier candidate is never replaced (#267), and two runs at once (CPU engines
    take no lease) never pick one name, which left one take and two runs reporting it (#591)."""
    for k in range(1, 10_000):
        p = out_dir / f"{stem}{'' if k == 1 else f'-{k}'}{suffix}"
        if any(os.path.lexists(p.with_suffix(s)) for s in (suffix, ".json", *extra)):
            continue
        try:
            os.close(os.open(p.with_suffix(".json"), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644))
        except FileExistsError:
            continue
        return p
    raise RuntimeError(f"{out_dir} holds 9,999 files named {stem}: pick another directory")


def publish(fresh: Path, out: Path, extra: tuple[str, ...] = ()) -> None:
    """Move a candidate made in its own temporary directory, its sidecar (and any ``extra``
    suffixes beside it, such as a spectrogram) to ``out``'s name, which `free` claimed: the
    sidecar first, so a file is never seen without its record."""
    os.replace(fresh.with_suffix(".json"), out.with_suffix(".json"))
    for suf in extra:
        if fresh.with_suffix(suf).is_file():
            os.replace(fresh.with_suffix(suf), out.with_suffix(suf))
    os.replace(fresh, out)
