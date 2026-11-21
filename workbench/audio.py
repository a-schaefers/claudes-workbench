"""Local audio generation (speech, sound effects, music), directed by Claude, who can't hear it.

Claude writes the brief, `wb audio` has a local audio model make a few candidates, and checks each
one in the ways Claude can read: its length against the one asked for, its peak and clipping, its
silences (a lead, a tail, a gap in the middle: a music model cut short), its loudness and a
spectrogram picture (with ffmpeg), and for speech a listener (a speech recognition engine) that
transcribes it back, scored against the script as a word error rate. Taste stays the user's: for
music and a voice's character Claude shortlists and asks the user to listen.

Engines are described in ~/.config/workbench/audio.json (`$WB_AUDIO`), name -> entry, shaped like
images.json: {"command": "<argv template>"} or {"url": "<an OpenAI /v1/audio/speech server>"},
plus "kind" (speech, sfx, music, or listen: the checker; default speech), "gpu" (false: it runs on
the CPU, without the GPU lease, so beside a task; a url on another host is off this GPU unless
true), "voices", "seconds" (the default length),
"unload" (a URL POSTed after a batch, so a server lets its GPU memory go), "key" (a url
engine's API key), "ref_arg", "model",
"label", "notes" (the licence: shown to Claude), "timeout" (s). Placeholders: {text} {out} {seed}
{seconds} {voice} {lyrics}, and {in} for a listener, which prints its transcript on stdout. No
model is named here: the user installs one and describes it.
"""
from __future__ import annotations

import array
import contextlib
import json
import math
import operator
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
import uuid
import wave
from pathlib import Path

from . import media, store

KINDS = ("speech", "sfx", "music")
PLACEHOLDERS = ("text", "out", "seed", "seconds", "voice", "lyrics")
DEFAULT_SECONDS = {"sfx": 5, "music": 30}
SILENT_DB = -50     # a 50 ms block quieter than this (dBFS) is silence
GAP_SECS = 1.0      # a silence this long between sounds is reported as a gap


def config_path() -> Path:
    return Path(os.environ.get("WB_AUDIO") or Path.home() / ".config/workbench/audio.json")


def kind_of(e: dict) -> str:
    return e.get("kind") or "speech"


def ready(e: dict) -> str:
    """'' when the engine can run, else why not; a kind it doesn't know is refused (#596)."""
    return media.ready(e, (*KINDS, "listen"), config_path().parent)


def engines() -> dict[str, dict]:
    return media.load(config_path())


def engine(name: str | None, kind: str | None) -> tuple[str, dict]:
    """The named engine, or the first of ``kind``, or the first that makes audio."""
    have = engines()
    makers = {k: e for k, e in have.items() if kind_of(e) != "listen"}
    if not makers:
        raise KeyError(f"no audio engine: describe one in {config_path()}")
    if name is not None:
        if name not in makers:
            raise KeyError(f"no audio engine {name!r} (have: {', '.join(makers)})")
        if kind and kind_of(makers[name]) != kind:
            raise KeyError(f"audio engine {name} makes {kind_of(makers[name])}, not {kind}")
        return name, makers[name]
    for k, e in makers.items():
        if kind is None or kind_of(e) == kind:
            return k, e
    raise KeyError(f"no {kind} engine in {config_path()} (have: "
                   + ", ".join(f"{k} ({kind_of(e)})" for k, e in makers.items()) + ")")


def listener() -> tuple[str, dict] | None:
    return next(((k, e) for k, e in engines().items() if kind_of(e) == "listen"), None)


def _takes(e: dict, slot: str) -> bool:
    return f"{{{slot}}}" in (e.get("command") or "")


# --- making a candidate -------------------------------------------------------------------------

def _command(e: dict, values: dict, refs: list[Path], log: Path) -> None:
    argv = media.fill(e["command"], {k: values[k] for k in PLACEHOLDERS})
    for r in refs:
        argv += [e["ref_arg"], str(r)]
    with open(log, "a") as f:
        r = media.run_group(argv, float(e.get("timeout", 1800)), stdin=subprocess.DEVNULL, stdout=f,
                            stderr=subprocess.STDOUT)
    if r.returncode or not Path(values["out"]).is_file():
        tail = "\n".join(log.read_text(errors="replace").strip().splitlines()[-8:])  # #743
        raise RuntimeError(f"the engine exited {r.returncode} without audio:\n{tail}")


def _server(e: dict, values: dict) -> None:
    """An OpenAI /v1/audio/speech server (audio.cpp's, Kokoro-FastAPI, Speaches…): WAV back."""
    body = {"input": values["text"], "response_format": "wav", "seed": values["seed"]}
    for k, v in (("model", e.get("model")), ("voice", values["voice"] or None)):
        if v:
            body[k] = v
    raw, _ = media.post(e, e["url"], json.dumps(body).encode(), "application/json",
                        float(e.get("timeout", 1800)), config_path().parent)
    Path(values["out"]).write_bytes(raw)


def _unload(used: list[dict], say) -> None:
    """The engine's and the listener's `unload` URLs, after the batch (`media.unload`, #594)."""
    media.unload(used, say, "audio", config_path().parent)


def generate(text: str, out_dir: Path, kind: str | None = None, n: int = 3,
             seconds: float | None = None, voice: str | None = None, lyrics: str = "",
             refs: list[Path] = (), seed: int | None = None, name: str | None = None,
             check: bool = True, say=print, wait: bool = False) -> list[Path]:
    """Make n candidates (seeds seed, seed+1, …), each a .wav with a .json sidecar holding how it
    was made and what the checks found (and a .png spectrogram when ffmpeg is here); returns
    their paths. A GPU engine (the default) takes the GPU as `wb image` does; ``gpu: false`` runs
    without it."""
    name, e = engine(name, kind)
    kind = kind_of(e)
    if why := ready(e):
        raise RuntimeError(f"audio engine {name}: {why}")
    refs = [Path(r).resolve() for r in refs]
    if refs and not (e.get("command") and e.get("ref_arg")):
        raise RuntimeError(f"audio engine {name} takes no reference clips (no ref_arg)")
    for r in refs:
        if not r.is_file():
            raise RuntimeError(f"no reference clip {r}")
    if voice and e.get("command") and not _takes(e, "voice"):
        raise RuntimeError(f"audio engine {name} takes no voice (no {{voice}} in its command)")
    if voice and e.get("voices") and voice not in e["voices"]:
        raise RuntimeError(f"audio engine {name} has no voice {voice!r} (voices: {', '.join(e['voices'])})")
    if lyrics and not _takes(e, "lyrics"):
        raise RuntimeError(f"audio engine {name} takes no lyrics (no {{lyrics}} in its command)")
    if seconds is not None and not _takes(e, "seconds"):
        raise RuntimeError(f"audio engine {name} takes no length (no {{seconds}} in its command)")
    if seconds is None and _takes(e, "seconds"):
        seconds = e.get("seconds") or DEFAULT_SECONDS.get(kind, 10)
    voice = voice or (e["voices"][0] if e.get("voices") else "")
    ear = listener() if check and kind == "speech" else None
    if ear and (why := ready(ear[1])):
        say(f"listener {ear[0]}: {why}; no transcripts")
        ear = None
    # absolute on every Python: 3.11's mkdtemp kept a relative dir relative, so an engine that
    # changed directory wrote its {out} elsewhere and the run failed (3.12+ passed) (#751)
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    seed = int(time.time()) % 1_000_000 if seed is None else seed
    on_gpu = media.on_gpu(e) or bool(ear and media.on_gpu(ear[1]))
    made = []
    hold = (media.gpu("wb audio", say, wait, media.serving(e, *([ear[1]] if ear else [])))
            if on_gpu else contextlib.nullcontext())
    with hold:
        try:
            for i in range(n):
                s = seed + i
                stem = f"{media.slug(text, kind)}-{s}"
                # made in a directory of its own, then published under a name nothing has (#267)
                with tempfile.TemporaryDirectory(prefix=store.scratch(".wb-audio-"), dir=out_dir) as tmp:
                    fresh = Path(tmp) / f"{stem}.wav"
                    values = {"text": text, "out": fresh, "seed": s, "seconds": seconds or "",
                              "voice": voice, "lyrics": lyrics}
                    t0 = time.time()
                    if e.get("command"):
                        _command(e, values, refs, out_dir / "engine.log")
                    else:
                        _server(e, values)
                    secs = round(time.time() - t0, 1)
                    if fresh.is_symlink() or not fresh.is_file() or not fresh.stat().st_size:
                        raise RuntimeError(f"audio engine {name} wrote no audio")
                    found = inspect(fresh, kind, text if kind == "speech" else "", seconds,
                                    ear, draw=check, log=out_dir / "engine.log")
                    if found.pop("not_audio", False):  # an error page published as a clip (#592)
                        raise RuntimeError(f"audio engine {name} wrote no audio ({media.starts(fresh)})")
                    store.write_json(fresh.with_suffix(".json"), {
                        "text": text, "kind": kind, "seed": s, "seconds": seconds, "voice": voice,
                        "lyrics": lyrics, "refs": [str(r) for r in refs], "engine": name,
                        "secs": secs, "ts": store.now(), **found})
                    out = media.free(out_dir, stem, ".wav", (".png",))
                    media.publish(fresh, out, (".png",))
                say(f"{out}  ({secs} s)\n  {summary(found, seconds)}"
                    + (f"\n  spectrogram: {out.with_suffix('.png')}" if out.with_suffix(".png").is_file() else ""))
                made.append(out)
        finally:  # inside the lease: the next task loads beside nothing
            _unload([e, *([ear[1]] if ear else [])], say)
    return made


# --- the checks ---------------------------------------------------------------------------------

# What an audio file starts with: one the wave module can't read (a float or 24-bit WAV, an MP3)
# may still be good when there is no ffmpeg to say; anything else isn't audio
MAGIC = (b"RIFF", b"RF64", b"FORM", b"fLaC", b"OggS", b"ID3", b"\x1aE\xdf\xa3", b"caff")


def _sounds(path: Path) -> bool:
    with open(path, "rb") as f:
        head = f.read(4)
    return head.startswith(MAGIC) or (len(head) > 1 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0)


class _Meter:
    """Duration, peak, clipping and silences, kept as running totals over 50 ms blocks of
    magnitudes (each frame's loudest channel): only one bool a block is held. Every sample as a
    Python float took ~200 MiB for a 5-minute clip, over 1 GiB at 48 kHz stereo (#599)."""

    def __init__(self, rate: int, full: float):
        self.rate, self.full, self.block = rate, full, max(1, rate // 20)
        self.frames, self.peak, self.clipped, self.loud = 0, 0.0, 0, []

    def add(self, mags) -> None:
        """One block's magnitudes, at most ``block`` of them."""
        if not len(mags):
            return
        self.frames += len(mags)
        self.peak = max(self.peak, max(mags))
        top = 0.999 * self.full
        self.clipped += sum(map(top.__le__, mags))  # x >= top, in C
        rms = math.sqrt(sum(map(operator.mul, mags, mags)) / len(mags)) / self.full
        self.loud.append(_db(rms) > SILENT_DB)

    def result(self) -> dict:
        loud, rate = self.loud, self.rate
        out = {"duration": round(self.frames / rate, 2) if rate else 0.0}
        if not self.frames:
            return {**out, "peak_db": -120.0, "clipped": 0.0, "lead": 0.0, "tail": 0.0, "gaps": []}
        peak, clipped = _db(self.peak / self.full), round(self.clipped / self.frames, 4)
        step = self.block / rate
        if not any(loud):
            return {**out, "peak_db": peak, "clipped": clipped, "lead": out["duration"],
                    "tail": 0.0, "gaps": [], "silent": True}
        first = loud.index(True)
        last = len(loud) - 1 - loud[::-1].index(True)
        gaps, run = [], 0
        for j in range(first, last + 1):
            if loud[j]:
                if run * step >= GAP_SECS:
                    gaps.append([round((j - run) * step, 1), round(run * step, 1)])  # [at, length]
                run = 0
            else:
                run += 1
        return {**out, "peak_db": peak, "clipped": clipped, "lead": round(first * step, 2),
                "tail": round((len(loud) - 1 - last) * step, 2), "gaps": gaps}


def _plain_tag(path: Path) -> None:
    """Mark ffmpeg's 16-bit copy plain PCM. For more than two channels ffmpeg writes
    WAVE_FORMAT_EXTENSIBLE (its samples plain PCM all the same), which the wave module reads only
    from Python 3.12: once the copy kept the clip's channels (#586), a 5.1 take read "unreadable"
    on 3.11, where the old mono copy had been measured."""
    try:
        with open(path, "r+b") as f:
            head = f.read(64)
            i = head.find(b"fmt ", 12)
            if head[:4] == b"RIFF" and head[8:12] == b"WAVE" and i >= 0 and head[i + 8:i + 10] == b"\xfe\xff":
                f.seek(i + 8)
                f.write(b"\x01\x00")
    except OSError:
        pass


def _measure(path: Path) -> dict | None:
    """`levels` of a PCM WAV at its own rate, each frame's loudest channel, read a block at a
    time; None when the wave module can't read it (float or 24-bit WAV: ffmpeg converts those
    first)."""
    codes = {1: "B", 2: "h", 4: "i"}
    try:
        with wave.open(str(path)) as w:
            ch, width, rate = w.getnchannels(), w.getsampwidth(), w.getframerate()
            if width not in codes or not ch or not rate:
                return None
            full = 128.0 if width == 1 else float(2 ** (8 * width - 1))
            meter = _Meter(rate, full)
            while raw := w.readframes(meter.block):
                a = array.array(codes[width])
                a.frombytes(raw[:len(raw) - len(raw) % (width * ch)])
                if sys.byteorder == "big" and width > 1:
                    a.byteswap()
                if width == 1:  # unsigned, 128 the zero
                    a = array.array("h", (x - 128 for x in a))
                # a list, not an array of the samples' type: abs(-32768) doesn't fit a signed
                # short, so a mono take touching full scale ended `wb audio` in an OverflowError
                mags = (list(map(abs, a)) if ch == 1 else
                        list(map(max, *(map(abs, a[c::ch]) for c in range(ch)))))
                meter.add(mags)
    except (wave.Error, EOFError, OSError):
        return None
    return meter.result()


def _db(x: float) -> float:
    return round(20 * math.log10(x), 1) if x > 0 else -120.0


def levels(samples: list[float], rate: int) -> dict:
    """Duration, peak, clipping and silences of samples in -1..1, from 50 ms blocks."""
    meter = _Meter(rate, 1.0)
    for i in range(0, len(samples), meter.block):
        meter.add([abs(x) for x in samples[i:i + meter.block]])
    return meter.result()


def _ffmpeg(*args: str, timeout: int = 300) -> subprocess.CompletedProcess | None:
    exe = shutil.which("ffmpeg")
    if not exe:
        return None
    try:
        return subprocess.run([exe, "-hide_banner", "-nostdin", *args], capture_output=True,
                              encoding="utf-8", errors="replace", timeout=timeout)  # #743
    except (OSError, subprocess.TimeoutExpired):
        return None


def loudness(path: Path) -> float | None:
    """Integrated loudness (LUFS, EBU R128) from ffmpeg's ebur128, None without ffmpeg."""
    r = _ffmpeg("-i", str(path), "-filter_complex", "ebur128", "-f", "null", "-")
    found = re.findall(r"\bI:\s*(-?[\d.]+|-inf) LUFS", r.stderr) if r else []
    if not found:
        return None
    return None if found[-1] == "-inf" else float(found[-1])


def spectrogram(path: Path, png: Path) -> bool:
    r = _ffmpeg("-v", "error", "-i", str(path), "-lavfi", "showspectrumpic=s=800x300:legend=1",
                "-y", str(png))
    return bool(r and r.returncode == 0 and png.is_file())


# Scripts written without spaces between words (Thai, Lao, Myanmar, Khmer, kana, CJK ideographs):
# each character is a unit, or one wrong particle in a Japanese clause scored the whole clause
# (WER 100%) and a perfect take and a bad one looked alike (#598). Hangul is spaced: words.
UNSPACED = re.compile("[\u0e00-\u0eff\u1000-\u109f\u1780-\u17ff\u3040-\u30ff\u31f0-\u31ff"
                      "\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff66-\uff9f\U00020000-\U0003ffff]")


def words(text: str) -> list[str]:
    """The units a listener is scored on: lower-case words (letters, digits, apostrophes, and
    their combining marks: `\\w` split Hindi and Thai at each vowel sign), and each character of a
    script written without spaces. NFC first: `café` with a combining accent scored a WER of 1.0
    against the same word precomposed (#747)."""
    out, cur, after_unit = [], "", False
    for c in unicodedata.normalize("NFC", text.lower()).replace("’", "'"):
        if unicodedata.category(c).startswith("M"):  # a vowel sign or tone mark: its base's
            if cur:
                cur += c
            elif after_unit:
                out[-1] += c
            continue
        if UNSPACED.match(c):
            out += [cur, c] if cur else [c]
            cur, after_unit = "", True
        elif c.isalnum() or c in "'_":
            cur, after_unit = cur + c, False
        else:
            if cur:
                out.append(cur)
            cur, after_unit = "", False
    return out + [cur] if cur else out


def _join(units: list[str]) -> str:
    """Units back into text: no space between two characters of a script written without them."""
    return "".join(("" if k == 0 or (UNSPACED.match(units[k - 1]) and UNSPACED.match(u)) else " ") + u
                   for k, u in enumerate(units))


def by_characters(script: str) -> bool:
    """Whether most of the script is written without spaces: its rate is then a character error
    rate, and the line says CER (#598)."""
    units = words(script)
    return sum(bool(UNSPACED.match(u)) for u in units) * 2 > len(units) if units else False


def _align(ref: list[str], hyp: list[str]) -> list[tuple[list[str], list[str], int]]:
    """The fewest edits (substitutions, deletions, insertions) from ``ref`` to ``hyp``, as runs
    of (script words, heard words, edits): 0 edits for a matched word, else a whole stretch of
    edits. difflib's longest-block matching paired an insert with a delete among repeated
    words: one wrong word in "go go go go" scored 100% (#593)."""
    n, m = len(ref), len(hyp)
    d = [list(range(m + 1))] + [[i] + [0] * m for i in range(1, n + 1)]
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            d[i][j] = min(d[i - 1][j - 1] + (ref[i - 1] != hyp[j - 1]), d[i - 1][j] + 1, d[i][j - 1] + 1)
    steps, i, j = [], n, m
    while i or j:  # back from the end: a match or substitution first, then a deletion
        if i and j and d[i][j] == d[i - 1][j - 1] + (ref[i - 1] != hyp[j - 1]):
            steps.append((ref[i - 1] == hyp[j - 1], [ref[i - 1]], [hyp[j - 1]]))
            i, j = i - 1, j - 1
        elif i and d[i][j] == d[i - 1][j] + 1:
            steps.append((False, [ref[i - 1]], []))
            i -= 1
        else:
            steps.append((False, [], [hyp[j - 1]]))
            j -= 1
    runs: list[list] = []
    for same, a, b in reversed(steps):
        if same or not runs or not runs[-1][2]:
            runs.append([a, b, 0 if same else 1])
        else:  # this edit extends the stretch before it
            runs[-1] = [runs[-1][0] + a, runs[-1][1] + b, runs[-1][2] + 1]
    return [tuple(r) for r in runs]


def _spans(script: str, heard: str) -> tuple[int, list[tuple[str, str, int]]]:
    """The script's words and where the listener heard something else, as (script, heard,
    edits) spans. A span that differs only in spacing or apostrophes ("boatswain's", "boat
    swain's") is a match: the first real run (#396) counted a perfect take of it as a miss."""
    ref, hyp = words(script), words(heard)
    flat = lambda ws: "".join(ws).replace("'", "")  # noqa: E731
    spans = [(_join(a), _join(b), k) for a, b, k in _align(ref, hyp) if k and flat(a) != flat(b)]
    return len(ref), spans


def wer(script: str, heard: str) -> float:
    """Word error rate: substitutions, deletions and insertions over the script's length, not
    capped (a reading twice as long as the script ranks below a lighter one). Read it with
    `differences`: a listener writes "2026" for "twenty twenty-six", and Claude can tell that
    from a fumble only by seeing the words."""
    n, spans = _spans(script, heard)
    if not n:
        return 0.0 if not words(heard) else 1.0
    return round(sum(k for _, _, k in spans) / n, 3)


def differences(script: str, heard: str, most: int = 4) -> list[str]:
    """Up to ``most`` spans as 'script → heard' ('…→ (nothing)' for a skipped stretch)."""
    return [f"{a or '(nothing)'} → {b or '(nothing)'}" for a, b, _ in _spans(script, heard)[1][:most]]


def _multipart(fields: dict, file: Path) -> tuple[bytes, str]:
    b = uuid.uuid4().hex
    parts = [f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
             for k, v in fields.items() if v]
    parts.append(f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="{file.name}"\r\n'
                 "Content-Type: audio/wav\r\n\r\n".encode() + file.read_bytes() + b"\r\n")
    return b"".join(parts) + f"--{b}--\r\n".encode(), f"multipart/form-data; boundary={b}"


def transcribe(ear: tuple[str, dict], clip: Path, log: Path) -> str:
    """The listener's transcript: a command's stdout ({in} the clip; JSON with "text", or
    audio.cpp's `text_output=` line, is read too), or an OpenAI /v1/audio/transcriptions
    server's "text"."""
    name, e = ear
    if e.get("command"):
        argv = media.fill(e["command"], {"in": clip})
        # decoded leniently: a listener that split a CJK character at a segment's end, or a
        # Windows program's code page through WSL, raised UnicodeDecodeError and lost the take (#743)
        r = media.run_group(argv, float(e.get("timeout", 600)), stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8",
                            errors="replace")
        with open(log, "a", encoding="utf-8") as f:
            f.write(r.stderr)
        if r.returncode:
            raise RuntimeError(f"listener {name} exited {r.returncode}: "
                               + " ".join(r.stderr.strip().splitlines()[-2:]))
        out = r.stdout.strip()
    else:
        body, ctype = _multipart({"model": e.get("model")}, clip)
        raw, _ = media.post(e, e["url"], body, ctype, float(e.get("timeout", 600)), config_path().parent)
        out = raw.decode(errors="replace").strip()
    try:
        data = json.loads(out)
        if isinstance(data, dict) and isinstance(data.get("text"), str):
            return data["text"].strip()
    except ValueError:
        pass
    # audio.cpp's CLI prints key=value lines, the words with their timestamps among them: scored
    # whole, a perfect take read as a WER of several hundred percent (the first real run, #396)
    said = [line.split("=", 1)[1] for line in out.splitlines() if line.startswith("text_output=")]
    return " ".join(said).strip() if said else out


def inspect(path: Path, kind: str, script: str = "", seconds: float | None = None,
            ear: tuple[str, dict] | None = None, draw: bool = True,
            log: Path | None = None) -> dict:
    """What Claude can read of a clip it can't hear. ffmpeg, when here, makes an integer PCM copy
    first (the wave module reads nothing else), measures loudness and draws the spectrogram beside
    the clip (.png). The levels are measured at the clip's own rate and channels: on a 16 kHz mono
    copy a stereo take clipped on one channel read clean, and one with a channel inverted read
    SILENT, its channels cancelled in the downmix (#586). The listener alone gets a 16 kHz mono copy."""
    found: dict = {}
    with tempfile.TemporaryDirectory(prefix=".wb-listen-") as tmp:
        whole = Path(tmp) / "pcm.wav"
        r = _ffmpeg("-v", "error", "-i", str(path), "-c:a", "pcm_s16le", "-y", str(whole))
        if r and r.returncode == 0 and whole.is_file():
            _plain_tag(whole)
        measured = _measure(whole if r and r.returncode == 0 and whole.is_file() else path)
        if measured is None:
            # ffmpeg couldn't decode it, or, with no ffmpeg, it doesn't even start like audio: an
            # engine's HTML error page was published as a clip and counted as made (#592)
            if (r is not None and r.returncode != 0) or (r is None and not _sounds(path)):
                found["not_audio"] = True
            found["unreadable"] = "not a PCM WAV the wave module reads" + ("" if r else "; no ffmpeg to convert it")
        else:
            found.update(measured)
        if r is not None:
            found["lufs"] = loudness(path)
            if draw:
                spectrogram(path, path.with_suffix(".png"))
        if ear and script:
            plain = Path(tmp) / "plain.wav"  # what listeners want
            r16 = _ffmpeg("-v", "error", "-i", str(path), "-ac", "1", "-ar", "16000", "-c:a",
                          "pcm_s16le", "-y", str(plain)) if r is not None else None
            clip = plain if r16 and r16.returncode == 0 and plain.is_file() else path
            try:
                heard = transcribe(ear, clip, log or Path(tmp) / "listen.log")
                found.update(transcript=heard, wer=wer(script, heard),
                             differences=differences(script, heard), listener=ear[0])
                if by_characters(script):
                    found["cer"] = True  # `wer` counts characters
            except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as err:
                found["listen_error"] = str(err)  # on the line, not raised (ValueError: #743)
    return found


def summary(found: dict, seconds: float | None) -> str:
    """One line of checks, for Claude."""
    if "unreadable" in found:
        bits = [f"unreadable: {found['unreadable']}"]
    else:
        d = found["duration"]
        bits = [f"{d:.1f} s" + (f" (asked {seconds:g}{', cut short' if d < 0.8 * float(seconds) else ''})"
                                 if seconds else "")]
        if found.get("silent"):
            bits.append("SILENT")
        bits.append(f"peak {found['peak_db']:g} dBFS"
                    + (f", clipped {found['clipped']:.1%}" if found["clipped"] >= 0.001 else ""))
        if found.get("lufs") is not None:
            bits.append(f"{found['lufs']:g} LUFS")
        elif "lufs" not in found:
            bits.append("loudness: no ffmpeg")
        bits.append(f"lead {found['lead']:g} s, tail {found['tail']:g} s")
        if found["gaps"]:
            bits.append("gaps " + ", ".join(f"{g[1]:g} s at {g[0]:g} s" for g in found["gaps"]))
    if "transcript" in found:
        bits.append(f"heard \"{found['transcript']}\" {'CER' if found.get('cer') else 'WER'} {found['wer']:.0%}"
                    + (f" ({'; '.join(found['differences'])})" if found.get("differences") else ""))
    elif "listen_error" in found:
        bits.append(f"listener failed: {found['listen_error']}")
    return " · ".join(bits)


def lines() -> list[str]:
    """Each engine for `wb audio --list` and `wb scout`: its kind, whether it's ready, its notes."""
    out = []
    for name, e in engines().items():
        why = ready(e)
        out.append(f"  {name} ({kind_of(e)}{'' if media.on_gpu(e) else ', CPU'}): "
                   f"{str(e.get('label') or e.get('command') or e.get('url') or '')[:60]}"
                   f" · {'ready' if not why else why}"
                   + (" · a server: it holds its GPU memory while it runs"
                      + ("" if e.get("unload") else " (no unload URL)")
                      if e.get("url") and not e.get("command") and media.on_gpu(e) else "")
                   + (f"\n    voices: {', '.join(e['voices'])}" if e.get("voices") and not why else "")
                   + (f"\n    {e['notes']}" if e.get("notes") else
                      "\n    licence: not noted (check it, then put it in notes)"))
    return out


def coverage() -> str | None:
    """What the engines cover, for the weekly model check (#398): each kind's engines, the kinds
    with none, and speech without a listener (its takes go unchecked)."""
    have = engines()
    if not have:
        return None
    by = {k: [n for n, e in have.items() if kind_of(e) == k] for k in (*KINDS, "listen")}
    line = "  covers " + "; ".join(f"{k}: {', '.join(v)}" for k, v in by.items() if v)
    if none := [k for k in KINDS if not by[k]]:
        line += f"; none for {', '.join(none)}"
    if by["speech"] and not by["listen"]:
        line += " · no listener: speech takes go unchecked"
    return line
