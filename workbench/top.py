"""`wb top`: a live feed of what this machine is doing for workbench: each GPU's memory, load and
power, the running tasks, every model (the rack) and the GPU lease's queue.

Cheap enough to run every 2 s, and nothing runs when nobody looks: no model call, no load, no
`opencode session export`. Each tick reads files (sysfs, gpu.json, the running tasks' status
and newest events) and the newest line of one long-lived `nvidia-smi -lms` child; the model
servers are asked every PROBE_EVERY seconds, not every tick. What a platform can't tell is left
out, never sent as zero.
"""
from __future__ import annotations

import json
import os
import re
import select
import shutil
import signal
import statistics
import subprocess
import sys
import time
from pathlib import Path

from . import hardware, models, router, stats, store
from .transcript import plain

EVERY = 2          # seconds between ticks without stats.json `every`
PROBE_EVERY = 5    # seconds between asks of the model servers (/api/ps, /api/tags, /v1/models, /metrics)
LOAD_EVERY = 300   # seconds between recomputing each model's typical load time
LIVE = ("queued", "running", "testing", "asking")
EVENTS_TAIL = 64 * 1024  # bytes of a running task's events.jsonl read for its newest steps
SMI_FIELDS = "index,memory.used,memory.total,utilization.gpu,power.draw"
MIB = 2 ** 20


def default_every() -> float:
    """stats.json `every` (`wb stats set every`: 1, 2 or 5 s), else EVERY."""
    return float(stats.settings().get("every") or EVERY)


def _float(text: str) -> float | None:
    try:
        return float(text)
    except ValueError:
        return None  # "[N/A]", "[Not Supported]"


def parse_smi(line: str) -> dict | None:
    """One line of the stream (SMI_FIELDS, no units) as {index, used, total, busy, watts}, each
    field left out where the card says [N/A]."""
    parts = [x.strip() for x in line.split(",")]
    if len(parts) != 5 or not parts[0].isdigit():
        return None
    used, total, busy, watts = (_float(x) for x in parts[1:])
    row = {"index": int(parts[0])}
    for k, v in (("used", used), ("total", total)):
        if v is not None:
            row[k] = int(v * MIB)
    if busy is not None:
        row["busy"] = int(busy)
    if watts is not None:
        row["watts"] = round(watts, 1)
    return row


class SmiStream:
    """One `nvidia-smi --query-gpu=… -lms <ms>` for the life of wb top: it prints a line per GPU
    every interval, read as it comes (never one nvidia-smi per tick)."""

    def __init__(self, path: str, every: float):
        self.ms = max(100, int(every * 1000))
        self.rows: dict[int, tuple[float, dict]] = {}
        self.buf = b""
        self.proc = subprocess.Popen(
            [path, f"--query-gpu={SMI_FIELDS}", "--format=csv,noheader,nounits", "-lms", str(self.ms)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        os.set_blocking(self.proc.stdout.fileno(), False)

    def fileno(self) -> int:
        return self.proc.stdout.fileno()

    def drain(self) -> None:
        while True:
            try:
                chunk = os.read(self.fileno(), 65536)
            except BlockingIOError:
                return
            except OSError:
                chunk = b""
            if not chunk:
                return
            self.buf += chunk
            *lines, self.buf = self.buf.split(b"\n")
            now = time.monotonic()
            for line in lines:
                row = parse_smi(line.decode(errors="replace"))
                if row:
                    self.rows[row["index"]] = (now, row)

    def latest(self) -> dict[int, dict]:
        """Each GPU's newest row, when it is fresh (three intervals): a stream that stopped gives nothing."""
        self.drain()
        fresh = time.monotonic() - max(3 * self.ms / 1000, 5)
        return {i: row for i, (at, row) in self.rows.items() if at >= fresh}

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.proc.stdout.close()


class _Files:
    """JSON files re-read only when they change (mtime and size): wb top looks at every task's
    status each tick, and most of them closed long ago."""

    def __init__(self):
        self.cache: dict[Path, tuple[tuple, object]] = {}

    def json(self, path: Path, default=None):
        try:
            st = path.stat()
        except OSError:
            self.cache.pop(path, None)
            return default
        key = (st.st_mtime_ns, st.st_size)
        hit = self.cache.get(path)
        if hit and hit[0] == key:
            return hit[1]
        data = store.read_json(path, default)
        self.cache[path] = (key, data)
        return data

    def events(self, path: Path) -> list:
        try:
            st = path.stat()
        except OSError:
            return []
        key = (st.st_mtime_ns, st.st_size)
        hit = self.cache.get(path)
        if hit and hit[0] == key:
            return hit[1]
        rows = store.read_jsonl_tail(path, EVENTS_TAIL)
        self.cache[path] = (key, rows)
        return rows


def _median(xs: list) -> float | None:
    return statistics.median(xs) if xs else None


class Top:
    """The state between ticks: the GPUs found at start, the stream, the cached files and the
    servers' last answers."""

    def __init__(self, every: float, drm: Path = hardware.DRM):
        self.every = every
        self.drm = drm
        self.files = _Files()
        self.where = hardware.system()
        self.smi: SmiStream | None = None
        self.names: dict = {}
        self.static: list[dict] = []  # GPUs read once: a Mac's, Windows' seen from WSL
        smi = hardware.smi_path() if self.where != "macos" else None
        if smi:
            for line in hardware._run(smi, "--query-gpu=index,name", "--format=csv,noheader").splitlines():
                i, _, name = line.partition(",")
                if i.strip().isdigit():
                    self.names[("nvidia", int(i))] = name.strip()
            try:
                self.smi = SmiStream(smi, every)
            except OSError:
                self.smi = None
        self.cards = [] if self.where == "macos" else \
            [c for c in hardware.drm_cards(drm) if not (smi and c["driver"] == "nvidia")]
        for c in self.cards:
            self.names[c["slot"]] = hardware.card_name(c)
        if self.where == "macos":
            self.static = [{"name": g["name"], **({"total": g["vram"]} if g.get("vram") else {}),
                            **({"unified": True} if g.get("unified") else {})}
                           for g in hardware._mac_gpus()]
        elif self.where == "wsl" and not self.smi and not self.cards:
            self.static = [{"name": g["name"]} for g in hardware._windows_gpus()]
        self.probed = 0.0
        self.loads_at = 0.0
        self.load: dict[str, float] = {}
        self.rack: list[dict] = []
        self.live: dict[str, dict] = {}  # server root -> its /metrics live object
        self.found: dict | None = None

    def close(self) -> None:
        if self.smi:
            self.smi.close()

    # ---- the GPUs ----

    def gpus(self) -> list[dict]:
        out = []
        if self.smi:
            for i, row in sorted(self.smi.latest().items()):
                out.append({"name": self.names.get(("nvidia", i), f"NVIDIA GPU {i}"),
                            "driver": "nvidia", **row})
        for n, c in enumerate(self.cards):
            dev = c["dev"]
            g = {"index": len(out) if self.smi else n, "name": self.names.get(c["slot"], c["card"]),
                 "driver": c["driver"]}
            used, total = hardware.vram(dev)
            if c["driver"] == "amdgpu" and not hardware.discrete(dev):
                gused, gtotal = hardware.gtt(dev)  # an APU: the carve-out and the GTT together
                if gtotal:
                    used = (used or 0) + (gused or 0) if used is not None or gused is not None else None
                    total, g["unified"] = (total or 0) + gtotal, True
            for k, v in (("used", used), ("total", total), ("busy", hardware.gpu_busy(dev)),
                         ("watts", hardware.card_watts(dev))):
                if v is not None:
                    g[k] = v
            out.append(g)
        out += [dict(g) for g in self.static]
        if not out:  # no GPU: the models run in RAM, so that is what fills
            m = hardware.memory() if self.where != "macos" else {}  # /proc/meminfo: one read
            if m.get("ram"):
                out.append({"name": "RAM (no GPU)", "ram": True, "total": m["ram"],
                            **({"used": m["ram"] - m["ram_free"]} if m.get("ram_free") is not None else {})})
        return out

    # ---- tasks ----

    def _tasks(self):
        """(name, spec, status) of every task in tasks/, from the cache."""
        try:
            dirs = sorted(store.tasks_dir().iterdir())
        except OSError:
            return
        for d in dirs:
            spec = self.files.json(d / "task.json")
            if isinstance(spec, dict):
                yield d, spec, self.files.json(d / "status.json", {}) or {}

    def running(self, found: dict, now: float) -> list[dict]:
        out = []
        for d, spec, st in self._tasks():
            if st.get("phase") not in LIVE:
                continue
            mid = spec.get("model") or ""
            m = found.get(mid)
            r = {"task": d.name, "model": mid, "label": spec.get("model_label") or (m.label if m else mid),
                 "phase": st["phase"]}
            if st.get("round"):
                r["round"] = st["round"]
            if st.get("phase") != "queued" and st.get("round_started_at"):
                r["round_s"] = round(now - st["round_started_at"], 1)
            if m and m.remote:
                r["remote"] = True
            live = self.live.get(m.root) if m and not m.remote else None
            if live and live.get("state") not in (None, "idle"):
                r["server"] = live
                if isinstance(live.get("tok_s"), (int, float)):
                    r["tok_s"], r["tok_s_from"] = round(live["tok_s"], 1), "live"
            if "tok_s" not in r and (last := self._last_call(d, st)):
                r["tok_s"], r["tok_s_from"] = last, "last call"
            sp = router.speed(mid) if mid else {}
            if sp.get("read"):
                r["read_tok_s"], r["read_from"] = round(sp["read"]), "median"
            used = (st.get("live_used") or {})
            window = st.get("window") or ((m.config.get("limit") or {}).get("context") if m else None)
            if used.get("used") and used.get("session") == st.get("session"):
                r["context_used"] = used["used"]
                if window:
                    r["context_window"] = window
                    r["context_pct"] = round(100 * used["used"] / window)
            if st.get("last"):
                r["last"] = str(st["last"])[:160]
            out.append(r)
        return out

    def _last_call(self, d: Path, st: dict) -> float | None:
        """The newest finished model call's writing rate, from the task's step events: its output
        over the time since the call before it (or the round's start). A floor: that time holds
        the prompt's reading and the tools' runs too."""
        rows = [r for r in self.files.events(d / "events.jsonl")
                if isinstance(r, dict) and r.get("kind") == "step" and r.get("round") == st.get("round")
                and isinstance(r.get("ts"), (int, float))]
        if not rows:
            return None
        last = rows[-1]
        since = rows[-2]["ts"] if len(rows) > 1 else st.get("round_started_at")
        gap = (last.get("ts") or 0) - (since or 0)
        if not since or gap < 0.5 or (last.get("out") or 0) < 20:
            return None
        return round(last["out"] / gap, 1)

    def _loads(self) -> dict[str, float]:
        """Each model's typical load here: the median of its tasks' load_secs (none measured: absent)."""
        if time.monotonic() - self.loads_at >= LOAD_EVERY or not self.loads_at:
            secs: dict[str, list] = {}
            for _, spec, st in self._tasks():
                if st.get("load_secs") and spec.get("model"):
                    secs.setdefault(spec["model"], []).append(st["load_secs"])
            self.load = {k: round(_median(v), 1) for k, v in secs.items()}
            self.loads_at = time.monotonic()
        return self.load

    # ---- the lease ----

    def pools(self) -> dict[str, dict]:
        """Every lease pool's live entries: gpu, and each remote server's gpu-<host>."""
        home = store.home()
        names = sorted({p.stem for p in home.glob("gpu*.json")} | {"gpu"})
        return {n: models.holders(home, n) for n in names}

    def queue(self, pools: dict, now: float) -> list[dict]:
        rows = [(h.get("since") or 0, pool, h) for pool, held in pools.items()
                for h in held.values() if h.get("waiting")]
        out = []
        for since, pool, h in sorted(rows, key=lambda x: x[0]):
            q = {"task": h.get("task"), "model": h.get("model")}
            if since:
                q["since"], q["waited_s"] = since, round(now - since, 1)
            if pool != "gpu":
                q["pool"] = pool
            out.append(q)
        return out

    # ---- the rack: every model, asked of its server every PROBE_EVERY seconds ----

    def _probe_due(self) -> bool:
        return time.monotonic() - self.probed >= PROBE_EVERY or not self.probed

    def probe(self, found: dict, running: list[dict]) -> None:
        self.probed = time.monotonic()
        fake = models._fake()
        tags, ps = {}, {}
        for m in found.values():  # one /api/tags and one /api/ps per local Ollama
            if m.engine == "ollama" and not m.remote and not fake and m.server not in tags:
                tags[m.server] = models._get(f"{m.root}/api/tags", timeout=1.5)
                ps[m.server] = (models._get(f"{m.root}/api/ps", timeout=1.5) or {}).get("models") or [] \
                    if tags[m.server] is not None else []
        # the live object of each running task's server (Ollama has no /metrics)
        roots = {found[r["model"]].root for r in running if r["model"] in found
                 and not found[r["model"]].remote and found[r["model"]].engine != "ollama"}
        self.live = {}
        for root in () if fake else roots:
            body = models._get(f"{root}/metrics", timeout=1.0)
            if isinstance(body, dict) and isinstance(body.get("live"), dict):
                self.live[root] = body["live"]
        self.rack = [self._state(m, tags, ps) for m in found.values()]

    def _state(self, m: models.Model, tags: dict, ps: dict) -> dict:
        r = {"model": m.id, "label": m.label}
        if m.remote:
            r.update(state="remote", why="another machine: no telemetry")
        elif m.engine != "ollama" or models._fake():
            st = models.status(m)
            r["state"] = ("resident" if st["resident"] else "idle") if st["available"] else "can't run"
            if not st["available"]:
                r["why"] = st["why"]
        else:
            # the config's own refusals first, as status() says them, without asking the server
            # (it is asked once per Ollama above): with its root as down, status() stops there
            st = models.status(m, down=frozenset({m.root}))
            row = models._tag_row(m.name, tags.get(m.server) or {}) if tags.get(m.server) is not None else None
            if st["why"] != "ollama is not answering":
                r.update(state="can't run", why=st["why"])
            elif tags.get(m.server) is None:
                r.update(state="can't run", why="ollama is not answering")
            elif row is None:
                r.update(state="can't run", why=f"{m.name} is not pulled")
            elif models._cloud_row(row) and not models.cloud_allowed(m.id):
                r.update(state="can't run", why=models.CLOUD_WHY.format(id=m.id))
            else:
                hit = next((x for x in ps.get(m.server) or [] if models._matches(m.name, {x.get("name")})), None)
                if hit is None:
                    r["state"] = "idle"
                else:
                    size, on_gpu = int(hit.get("size") or 0), int(hit.get("size_vram") or 0)
                    r["state"] = "resident" if size and on_gpu >= size else "split" if size else "resident"
                    if on_gpu or size:
                        r["gpu_bytes"] = on_gpu
                    if size > on_gpu:
                        r["cpu_bytes"] = size - on_gpu
        return r

    def rack_now(self, pools: dict) -> list[dict]:
        held: dict[str, list] = {}
        for h in (h for p in pools.values() for h in p.values() if not h.get("waiting")):
            if h.get("model") and h.get("task"):
                held.setdefault(h["model"], []).append(h["task"])
        loads = self._loads()
        out = []
        for r in self.rack:
            r = dict(r)
            sp = router.speed(r["model"])
            if sp.get("write"):
                r["write_tok_s"] = round(sp["write"], 1)
            if sp.get("read"):
                r["read_tok_s"] = round(sp["read"])
            if r["model"] in loads:
                r["load_s"] = loads[r["model"]]
            if r["model"] in held:
                r["tasks"] = held[r["model"]]
            out.append(r)
        return out

    def tick(self) -> dict:
        now = time.time()
        if self._probe_due() or self.found is None:
            self.found = models.discover()  # the config, re-read with the servers' answers
        found = self.found
        running = self.running(found, now)
        if self._probe_due():
            self.probe(found, running)
            running = self.running(found, now)  # with the servers' live objects
        pools = self.pools()
        return {"ts": round(now, 1), "every": self.every, "gpus": self.gpus(), "running": running,
                "rack": self.rack_now(pools), "queue": self.queue(pools, now)}


# ---- the terminal view ----

BLOCKS = "▏▎▍▌▋▊▉█"
COLOURS = {"head": "1", "dim": "2", "ok": "32", "warn": "33", "bad": "31", "name": "1;36"}


def bar(frac: float, width: int = 16) -> str:
    """``frac`` (0..1) of ``width`` cells, in eighths."""
    eighths = round(max(0.0, min(1.0, frac)) * width * 8)
    full, part = divmod(eighths, 8)
    return ("█" * full + (BLOCKS[part - 1] if part else "")).ljust(width)


def _gb(n) -> str:
    return f"{n / 2 ** 30:.1f}"


def _secs(s: float) -> str:
    s = int(s)
    return f"{s}s" if s < 60 else f"{s // 60}m{s % 60:02d}s" if s < 3600 else f"{s // 3600}h{s % 3600 // 60:02d}m"


def short(name: str) -> str:
    """lspci's long name cut to the card's own (its last bracket: Radeon RX 9070 XT), for the view."""
    groups = re.findall(r"\[([^\]]+)\]", name)
    return groups[-1] if groups else name


def lines(snap: dict) -> list[list[tuple[str, str]]]:
    """The view as lines of (style, text) segments: the text is safe to print (plain)."""
    p = lambda v: plain(str(v))  # noqa: E731
    out = [[("head", f"wb top · {time.strftime('%H:%M:%S', time.localtime(snap['ts']))} · every "
             f"{snap['every']:g}s"), ("dim", "   q quits")], []]
    for g in snap["gpus"]:
        row = [("name", f"{'RAM' if g.get('ram') else 'GPU ' + str(g.get('index', ''))}".ljust(6)),
               ("", p(short(g["name"]))[:30].ljust(31))]
        if g.get("used") is not None and g.get("total"):
            frac = g["used"] / g["total"]
            row += [("bad" if frac > 0.95 else "warn" if frac > 0.8 else "ok", bar(frac)),
                    ("", f" {_gb(g['used'])}/{_gb(g['total'])} GB  ")]
        elif g.get("total"):
            row += [("dim", f"{_gb(g['total'])} GB{' unified' if g.get('unified') else ''}, use not said  ")]
        if g.get("busy") is not None:
            row += [("dim", "busy "), ("warn" if g["busy"] > 80 else "ok", bar(g["busy"] / 100, 8)),
                    ("", f" {g['busy']:>3}%  ")]
        if g.get("watts") is not None:
            row += [("", f"{g['watts']:.0f} W")]
        out.append(row)
    out += [[], [("head", "RUNNING")]]
    for r in snap["running"]:
        row = [("name", f"  {p(r['task'])}"), ("", f"  {p(r['label'])}  "), ("warn", r["phase"])]
        if r.get("round"):
            row.append(("", f" r{r['round']}"))
        if r.get("round_s") is not None:
            row.append(("dim", f" {_secs(r['round_s'])}"))
        if r.get("tok_s") is not None:
            row.append(("", f"  {r['tok_s']:.0f} tok/s {'live' if r['tok_s_from'] == 'live' else '(last call)'}"))
        if r.get("read_tok_s"):
            row.append(("dim", f" · reads ~{r['read_tok_s']}"))
        if r.get("context_used"):
            ctx = f"  context {r['context_used'] / 1000:.1f}K"
            if r.get("context_window"):
                ctx += f"/{r['context_window'] // 1024}K {r['context_pct']}%"
            row.append(("", ctx))
        out.append(row)
        if r.get("last"):
            out.append([("dim", f"    {p(r['last'])}")])
    if not snap["running"]:
        out.append([("dim", "  nothing running")])
    out += [[], [("head", "RACK")]]
    for r in snap["rack"]:
        state = r["state"]
        style = {"resident": "ok", "split": "warn", "can't run": "bad"}.get(state, "dim")
        where = ""
        if r.get("gpu_bytes") is not None:
            where = f" {_gb(r['gpu_bytes'])} GB GPU" + (f" + {_gb(r['cpu_bytes'])} GB CPU" if r.get("cpu_bytes") else "")
        row = [("name", f"  {p(r['label'])[:30].ljust(31)}"), (style, f"{state}{where}".ljust(31))]
        facts = [f"writes ~{r['write_tok_s']:.0f}" if r.get("write_tok_s") else "",
                 f"reads ~{r['read_tok_s']}" if r.get("read_tok_s") else "",
                 f"loads ~{_secs(r['load_s'])}" if r.get("load_s") else ""]
        row.append(("dim", " · ".join(f for f in facts if f)))
        if r.get("tasks"):
            row.append(("", "  ← " + ", ".join(p(t) for t in r["tasks"])))
        if r.get("why") and state != "remote":
            row.append(("dim", f"  {p(r['why'])}"))
        elif state == "remote":
            row.append(("dim", "  another machine: no telemetry"))
        out.append(row)
    if not snap["rack"]:
        out.append([("dim", "  no models found")])
    out += [[], [("head", "QUEUE")]]
    for q in snap["queue"]:
        out.append([("", f"  {p(q['task'])}  {p(q.get('model') or '')}"),
                    ("dim", f"  waiting {_secs(q['waited_s'])}" if q.get("waited_s") is not None else "")])
    if not snap["queue"]:
        out.append([("dim", "  nobody waiting")])
    return out


def render(snap: dict, width: int, color: bool) -> str:
    """The lines cut to ``width`` columns, coloured on a terminal."""
    rows = []
    for segs in lines(snap):
        left, parts = width, []
        for style, text in segs:
            if left <= 0:
                break
            text = text[:left]
            left -= len(text)
            parts.append(f"\033[{COLOURS[style]}m{text}\033[0m" if color and style and text else text)
        rows.append("".join(parts))
    return "\n".join(rows)


# ---- running it ----

def main(every: float | None = None, as_json: bool = False, out=None, drm: Path = hardware.DRM) -> None:
    """The loop: a tick every ``every`` seconds until its parent goes, SIGTERM, q or Ctrl-C.
    Not on a terminal and without ``as_json``: one pass, printed plain."""
    out = out or sys.stdout
    every = every or default_every()
    tty = out.isatty() and not as_json
    parent = os.getppid()

    def end(_signum, _frame):
        raise SystemExit(0)
    old = {s: signal.signal(s, end) for s in (signal.SIGTERM, signal.SIGHUP)}
    top = Top(every, drm=drm)
    keys = None
    saved = None
    try:
        if tty:
            try:
                import termios
                import tty as tty_mod
                if sys.stdin.isatty():
                    saved = termios.tcgetattr(sys.stdin.fileno())
                    tty_mod.setcbreak(sys.stdin.fileno())
                    keys = sys.stdin.fileno()
            except (ImportError, OSError):
                keys = None
            out.write("\033[?1049h\033[?25l")  # the alternate screen, the cursor hidden
        while True:
            started = time.monotonic()
            snap = top.tick()
            if as_json:
                out.write(json.dumps(snap, ensure_ascii=False) + "\n")
                out.flush()
            elif tty:
                size = shutil.get_terminal_size()
                text = render(snap, size.columns, color=True).split("\n")[:size.lines]
                out.write("\033[H" + "".join(f"{line}\033[K\n" for line in text[:-1])
                          + (text[-1] + "\033[K" if text else "") + "\033[J")
                out.flush()
            else:
                out.write(render(snap, 10 ** 6, color=False) + "\n")
                out.flush()
                return
            while (left := started + every - time.monotonic()) > 0:
                # a stream that ended reads as ready forever: no longer waited on (its rows go stale)
                smi = top.smi.fileno() if top.smi and top.smi.proc.poll() is None else None
                fds = [f for f in (smi, keys) if f is not None]
                if not fds:
                    time.sleep(left)
                    break
                ready = select.select(fds, [], [], left)[0]
                if smi is not None and smi in ready:
                    top.smi.drain()
                if keys is not None and keys in ready:
                    if "q" in os.read(keys, 64).decode(errors="replace").lower():
                        return
            if os.getppid() != parent:  # whoever started it is gone: nobody looks
                return
    except KeyboardInterrupt:
        return
    except BrokenPipeError:  # the reader went away
        sys.stdout = open(os.devnull, "w")
    finally:
        top.close()
        if tty:
            try:
                out.write("\033[?25h\033[?1049l")
                out.flush()
            except (BrokenPipeError, OSError):
                pass
        if saved is not None:
            import termios
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, saved)
        for s, h in old.items():
            signal.signal(s, h)
