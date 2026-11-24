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
import threading
import time
from pathlib import Path

from . import hardware, meter, models, router, stats, store
from .transcript import plain
from .savings import money

EVERY = 2          # seconds between ticks without stats.json `every`
PROBE_EVERY = 5    # seconds between asks of the model servers (/api/ps, /api/tags, /v1/models, /metrics)
LOAD_EVERY = 300   # seconds between recomputing each model's typical load time
LIVE = ("queued", "running", "testing", "asking")
EVENTS_TAIL = 64 * 1024  # bytes of a running task's events.jsonl read for its newest steps
SMI_FIELDS = "index,memory.used,memory.total,utilization.gpu,power.draw"
SMI_RETRY_MAX = 60  # seconds: a stream that keeps exiting is started again at most this rarely
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
        self.ended = False  # its stdout reached EOF
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
                self.ended = True
                return
            self.buf += chunk
            *lines, self.buf = self.buf.split(b"\n")
            now = time.monotonic()
            for line in lines:
                row = parse_smi(line.decode(errors="replace"))
                if row:
                    self.rows[row["index"]] = (now, row)

    def wait(self, timeout: float) -> None:
        """Until its first row, the stream's end or ``timeout`` seconds: read before nvidia-smi's
        first line, an NVIDIA machine's one pass said "RAM (no GPU)" (#547)."""
        end = time.monotonic() + timeout
        while not self.rows and not self.ended and (left := end - time.monotonic()) > 0:
            select.select([self.fileno()], [], [], left)
            self.drain()

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
        self.costs: dict[Path, tuple[int, int, float]] = {}  # step_cost's (inode, offset, sum)

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

    def step_cost(self, path: Path) -> float:
        """The step events' summed `cost` over the whole events.jsonl (results.step_cost), read on
        from where the last tick stopped: the EVENTS_TAIL that events() keeps for the newest steps
        held a fraction of a long API task's spend (2000 steps: $0.22 of $2.00, #1022's review)."""
        try:
            st = path.stat()
        except OSError:
            self.costs.pop(path, None)
            return 0.0
        ino, off, total = self.costs.get(path, (st.st_ino, 0, 0.0))
        if ino != st.st_ino or st.st_size < off:  # replaced or cut short: summed again
            off, total = 0, 0.0
        if st.st_size > off:
            from . import results
            try:
                with open(path, "rb") as fh:
                    fh.seek(off)
                    chunk = fh.read()
            except OSError:
                return total
            end = chunk.rfind(b"\n") + 1  # a line still being written waits for the next tick
            rows = []
            for line in chunk[:end].splitlines():
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    pass
            total += results.step_cost(rows)
            off += end
        self.costs[path] = (st.st_ino, off, total)
        return total


def _median(xs: list) -> float | None:
    return statistics.median(xs) if xs else None


class Top:
    """The state between ticks: the GPUs found at start, the stream, the cached files and the
    servers' last answers."""

    def __init__(self, every: float, drm: Path = hardware.DRM, background: bool = False):
        self.every = every
        self.background = background  # the servers asked off the tick (the live loop's)
        self.prober: threading.Thread | None = None
        self.drm = drm
        self.files = _Files()
        self.where = hardware.system()
        self.smi: SmiStream | None = None
        self.names: dict = {}
        self.static: list[dict] = []  # GPUs read once: a Mac's, Windows' seen from WSL
        self.smi_path = smi = hardware.smi_path() if self.where != "macos" else None
        self.smi_wait, self.smi_retry = every, 0.0  # the stream's restart backoff
        if smi:
            for line in hardware._run(smi, "--query-gpu=index,name", "--format=csv,noheader").splitlines():
                i, _, name = line.partition(",")
                if i.strip().isdigit():
                    self.names[("nvidia", int(i))] = name.strip()
            self._start_smi()
            if self.smi:
                self.smi.wait(every)
        # the NVIDIA cards nvidia-smi named are its rows; one that named none leaves them to sysfs
        self.cards = [] if self.where == "macos" else \
            [c for c in hardware.drm_cards(drm) if not (self.names and c["driver"] == "nvidia")]
        for c in self.cards:
            self.names[c["slot"]] = hardware.card_name(c)
        self.mac: dict = {}  # macOS: hw.pagesize and hw.memsize read once, the RAM in use per probe
        if self.where == "macos":
            self.mac = {"page": hardware._int(hardware._sysctl("hw.pagesize")) or 4096,
                        "ram": hardware._int(hardware._sysctl("hw.memsize")) or None, "at": None}
            # an Apple GPU's memory is the RAM: its bar is the RAM in use against all of it, and
            # the most the GPU may wire (iogpu.wired_limit_mb, else macOS's default share, the
            # budget's ceiling, #574) is said beside it as `limit`. The RAM in use drawn against
            # that limit put an idle 16 GB Mac at 104% in red with no model loaded (#701)
            # an Intel Mac's Radeon: no total, as Ollama runs there on the CPU in RAM, so its
            # models read `cpu`, not a warning's "split 0.0 GB GPU" (#711)
            self.static = [{"name": g["name"], **({"total": g["vram"]} if g.get("vram") and g.get("unified") else {}),
                            **({"unified": True, "limit": g.get("wired_limit") or models.unified_default(g["vram"])}
                               if g.get("unified") and g.get("vram") else
                               {"unified": True} if g.get("unified") else {})}
                           for g in hardware._mac_gpus()]
        elif self.where == "wsl" and not self.names and not self.cards:  # a failing nvidia-smi too (#547)
            self.static = [{"name": g["name"], **({"total": g["vram"]} if g.get("vram") else {})}
                           for g in hardware._windows_gpus()]
        self.probed = 0.0
        self.reaped = 0.0
        self.loads_at = 0.0
        self.load: dict[str, float] = {}
        self.rack: list[dict] = []
        self.live: dict[str, dict] = {}  # server root -> its /metrics live object
        self.found: dict | None = None
        self.gpu_memory = True  # a GPU row says a memory size (gpus): else a model in RAM is `cpu`

    def close(self) -> None:
        if self.smi:
            self.smi.close()

    def _mac_used(self) -> int | None:
        """A Mac's RAM in use (hw.memsize less vm_stat's free, inactive and speculative pages),
        asked every PROBE_EVERY seconds, not every tick."""
        m = self.mac
        if m.get("ram") and (m["at"] is None or time.monotonic() - m["at"] >= PROBE_EVERY):
            free = hardware.mac_free(m["page"])
            m["used"], m["at"] = (m["ram"] - free if free is not None else None), time.monotonic()
        return m.get("used")

    def _start_smi(self) -> None:
        """Start the stream, or start it again once it has ended, backing off (every interval,
        doubling to SMI_RETRY_MAX): a driver reset or an Xid error ended it for the session (#547)."""
        now = time.monotonic()
        if not self.smi_path or (self.smi and self.smi.proc.poll() is None) or now < self.smi_retry:
            return
        if self.smi:
            self.smi.close()
            self.smi_wait = min(self.smi_wait * 2, SMI_RETRY_MAX)
        try:
            self.smi = SmiStream(self.smi_path, self.every)
        except OSError:
            self.smi = None
        self.smi_retry = now + self.smi_wait

    # ---- the GPUs ----

    def gpus(self) -> list[dict]:
        out = []
        self._start_smi()
        latest = self.smi.latest() if self.smi else {}
        if latest:
            self.smi_wait = self.every
        # every card nvidia-smi named stays a row, its readings left out and `stale` set while it
        # has none: missing telemetry read as missing hardware, "RAM (no GPU)" (#547)
        for i in sorted({i for _, i in (k for k in self.names if isinstance(k, tuple))} | set(latest)):
            out.append({"name": self.names.get(("nvidia", i), f"NVIDIA GPU {i}"), "driver": "nvidia",
                        "index": i, **latest.get(i, {}), **({} if i in latest else {"stale": True})})
        if any("total" not in g for g in out) and hardware.shares_ram():
            # a DGX Spark, a Jetson: nvidia-smi gives the GPU no memory ([N/A]), it is the RAM's
            # (hardware.gpus' rule), so the view showed none of it filling (#572). One read a tick
            m = hardware.memory()
            for g in out if m.get("ram") else ():
                if "total" not in g:
                    g.update(total=m["ram"], unified=True,
                             **({"used": m["ram"] - m["ram_free"]} if m.get("ram_free") is not None else {}))
        for n, c in enumerate(self.cards):
            dev = c["dev"]
            g = {"index": len(out) if self.smi_path else n, "name": self.names.get(c["slot"], c["card"]),
                 "driver": c["driver"]}
            used, total = hardware.vram(dev)
            if c["driver"] == "amdgpu" and hardware.apu(dev):
                gused, gtotal = hardware.gtt(dev)  # an APU: the carve-out and the GTT together
                if gtotal:
                    used = (used or 0) + (gused or 0) if used is not None or gused is not None else None
                    total, g["unified"] = (total or 0) + gtotal, True
            for k, v in (("used", used), ("total", total), ("busy", hardware.gpu_busy(dev)),
                         ("watts", hardware.card_watts(dev))):
                if v is not None:
                    g[k] = v
            if any(k in g for k in ("used", "total", "busy", "watts")):  # one that reports nothing
                # (an iGPU on i915, a BMC's ast, a VM's virtio-gpu) is no row (#551)
                out.append(g)
        out += [dict(g) for g in self.static]
        if self.where == "macos" and any(g.get("unified") for g in out) and (used := self._mac_used()) is not None:
            for g in out:
                if g.get("unified"):
                    g["used"] = used
        # no GPU says a memory size: the models run in RAM, so that is what fills. Shown only while
        # there were no rows at all, any display adapter hid it (#551); a card that is merely
        # unread just now (stale) keeps it away (#547)
        self.gpu_memory = any(g.get("total") for g in out)
        if not self.gpu_memory and not any(g.get("stale") for g in out):
            # /proc/meminfo: one read; on macOS what was read once and vm_stat per probe (#574)
            m = hardware.memory() if self.where != "macos" else \
                {"ram": self.mac.get("ram"), "ram_free": self.mac["ram"] - used
                 if self.mac.get("ram") and (used := self._mac_used()) is not None else None}
            if m.get("ram"):
                out.append({"name": "RAM" if out else "RAM (no GPU)", "ram": True, "total": m["ram"],
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

    def reap(self) -> None:
        """Fail every live task whose runner died without a word (cli.reap, as `wb events` does),
        every PROBE_EVERY seconds: a runner OOM-killed mid-round stayed `running`, its round clock
        counting, for as long as wb top was the only thing looking (#571). reap waits out
        REAP_GRACE and checks the pid's process itself."""
        if self.reaped and time.monotonic() - self.reaped < PROBE_EVERY:
            return
        self.reaped = time.monotonic()
        from . import cli  # wb top runs from cli: loaded already
        for d, _spec, st in list(self._tasks()):
            if st.get("phase") in LIVE:
                try:
                    cli.reap(store.Task(d.name))
                except OSError:
                    pass  # a task removed mid-tick; a full disk reaps on a later pass

    def _owners(self, found: dict) -> dict[str, int]:
        """How many tasks are in phase running on each local server root."""
        n: dict[str, int] = {}
        for _, spec, st in self._tasks():
            m = found.get(spec.get("model") or "")
            if st.get("phase") == "running" and m and not m.remote:
                n[m.root] = n.get(m.root, 0) + 1
        return n

    def running(self, found: dict, now: float) -> list[dict]:
        out = []
        # a server's live rate is a task's only when it is the one task running on that server: a
        # queued, testing or asking task, or one of two running there, claimed whatever the server
        # was writing, someone else's request too (#548). Unclaimed, it goes on the rack (rack_now)
        owners = self._owners(found)
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
            live = self.live.get(m.root) if m and not m.remote and st["phase"] == "running" \
                and owners.get(m.root) == 1 else None
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
            r.update(self._api(d, spec, st))
            out.append(r)
        return out

    def _api(self, d: Path, spec: dict, st: dict) -> dict:
        """An API task's part of its RUNNING row (#1022): `via`, its provider, `spend` (dollars so
        far; absent when unknown, never 0 for a missing price) and, queued, the provider slot it
        waits for. It takes no lease and no model of the rack: it is never in either."""
        if spec.get("via") != "api":
            return {}
        from . import results
        mid = str(spec.get("model") or "")
        pid = spec.get("provider") or mid.split("/", 1)[0]
        out = {"via": "api", "provider": pid}
        spend = results.live_spend(mid, st, self.files.step_cost(d / "events.jsonl"))
        if spend is not None:
            out["spend"] = spend
            out["prices"] = results.live_basis(st)  # its `≈` unless at the user's prices (#1135)
        if st.get("phase") == "queued":
            out["waits"] = f"a {pid} slot"
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
        """Each model's typical load here: the median of its tasks' loads, one by one (a task's
        load_secs is their total, reloads and all, #577; none measured: absent)."""
        if time.monotonic() - self.loads_at >= LOAD_EVERY or not self.loads_at:
            secs: dict[str, list] = {}
            for _, spec, st in self._tasks():
                if spec.get("model") and (one := models.single_loads(st)):
                    secs.setdefault(spec["model"], []).extend(one)
            self.load = {k: round(_median(v), 1) for k, v in secs.items()}
            self.loads_at = time.monotonic()
        return self.load

    # ---- the lease ----

    def pools(self) -> dict[str, dict]:
        """Every lease pool's live entries: gpu, and each remote server's gpu-<host>."""
        home = models.lease_dir()  # shared with other accounts when the machine opts in (#393)
        names = sorted({p.stem for p in home.glob("gpu*.json")} | {"gpu"})
        return {n: models.holders(home, n) for n in names}

    def queue(self, pools: dict, now: float) -> list[dict]:
        rows = [(h.get("since") or 0, pool, h) for pool, held in pools.items()
                for h in held.values() if h.get("waiting")]
        out = []
        for since, pool, h in sorted(rows, key=lambda x: x[0]):  # in line by the task's start
            q = {"task": h.get("task"), "model": h.get("model")}
            if since:
                q["since"] = since
            # the wait's own length, not the task's age (#573); an entry from before has none
            if isinstance(began := h.get("waiting_since"), (int, float)):
                q["waited_s"] = round(max(0.0, now - began), 1)
            if pool != "gpu":
                q["pool"] = pool
            if h.get("uid") is not None and h["uid"] != os.getuid():  # another account's (#393)
                q["task"] = f"{h.get('task')} ({h.get('user') or h['uid']})"
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
        live = {}  # built whole, then swapped in: the tick may read it meanwhile (_probe_bg)
        for root in () if fake else roots:
            body = models._get(f"{root}/metrics", timeout=1.0)
            if isinstance(body, dict) and isinstance(body.get("live"), dict):
                live[root] = body["live"]
        self.live = live
        catalog: dict = {}  # each other server's /v1/models, asked once for all its entries (#561)
        self.rack = [self._state(m, tags, ps, catalog) for m in found.values()]

    def _probe_bg(self, found: dict, running: list[dict]) -> None:
        """The probe on a thread of its own: a server that accepts and then stalls held the tick,
        so `q`, the nvidia-smi stream and the pane's lines waited for its timeouts (#561). The
        tick shows the newest answers; one probe runs at a time. The first is waited for, up to
        one interval (PROBE_EVERY at most), so the first view has the rack."""
        if self.prober and self.prober.is_alive():
            return
        self.probed = time.monotonic()
        first = self.prober is None
        self.prober = threading.Thread(target=self.probe, args=(found, running), daemon=True)
        self.prober.start()
        if first:
            self.prober.join(min(max(self.every, 1.0), PROBE_EVERY))  # --every may be an hour

    def _state(self, m: models.Model, tags: dict, ps: dict, catalog: dict | None = None) -> dict:
        r = {"model": m.id, "label": m.label}
        if m.remote:
            r.update(state="remote", why="another machine: no telemetry")
        elif m.engine != "ollama" or models._fake():
            st = models.status(m, catalog=catalog)
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
            elif models._cloud_row(row):  # a cloud model under a plain name: never a GPU one (#1013)
                r.update(state="can't run", why=models.CLOUD_WHY)
            else:
                hit = next((x for x in ps.get(m.server) or [] if models._matches(m.name, {x.get("name")})), None)
                if hit is None:
                    r["state"] = "idle"
                else:
                    size, on_gpu = int(hit.get("size") or 0), int(hit.get("size_vram") or 0)
                    if size and not on_gpu and not self.gpu_memory:
                        # all in RAM with no GPU memory here: the normal state of a CPU-only
                        # machine, not a warning's "split 0.0 GB GPU" (#551)
                        r.update(state="cpu", cpu_bytes=size)
                        return r
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
        # a busy server whose rate no task row claimed (two running there, or a request from outside
        # workbench) shows it once, on its first model's row, the resident one first (#548)
        owners = self._owners(self.found or {})
        unclaimed = {root: live for root, live in self.live.items()
                     if live.get("state") not in (None, "idle") and owners.get(root) != 1}
        rows = sorted(self.rack, key=lambda r: r.get("state") != "resident")
        shown: dict[str, str] = {}
        for r in rows:
            m = (self.found or {}).get(r["model"])
            if m and m.root in unclaimed and m.root not in shown:
                shown[m.root] = r["model"]
        out = []
        for r in self.rack:
            r = dict(r)
            if (m := (self.found or {}).get(r["model"])) and shown.get(m.root) == r["model"]:
                r["server"] = unclaimed[m.root]
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
        self.reap()
        gpus = self.gpus()  # first: the rack's states read gpu_memory
        if self._probe_due() or self.found is None:
            self.found = models.discover()  # the config, re-read with the servers' answers
        found = self.found
        running = self.running(found, now)
        if self._probe_due():
            if self.background:
                self._probe_bg(found, running)
            else:
                self.probe(found, running)
            running = self.running(found, now)  # with the servers' live objects
        pools = self.pools()
        return {"ts": round(now, 1), "every": self.every, "gpus": gpus, "running": running,
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
    return f"{n / 1e9:.1f}"  # decimal GB, as everything else wb prints (#523)


def _secs(s: float) -> str:
    s = int(s)
    return f"{s}s" if s < 60 else f"{s // 60}m{s % 60:02d}s" if s < 3600 else f"{s // 3600}h{s % 3600 // 60:02d}m"


def short(name: str) -> str:
    """lspci's long name cut to the card's own, for the view: its last bracket when the name ends
    with one (Navi 48 [Radeon RX 9070 XT]), else what follows the last bracket, which is the
    vendor's (Phoenix1, Device 1586 after [AMD/ATI], which a product with no bracket in pci.ids
    and a card newer than hwdata were shown as, #576); "(rev c4)" dropped. The pane's cardName
    is the same rule."""
    name = re.sub(r"\s*\(rev [0-9a-fA-F]+\)\s*$", "", name).strip()
    if name.endswith("]") and (groups := re.findall(r"\[([^\]]+)\]", name)):
        return groups[-1]
    return name.rpartition("]")[2].strip() or name


FOLD = ("idle", "can't run", "remote")  # rack rows folded first on a short terminal


def lines(snap: dict, rows: int | None = None) -> list[list[tuple[str, str]]]:
    """The view as lines of (style, text) segments: the text is safe to print (plain). ``rows``:
    the terminal's height. Taller, the rack's idle, can't-run and remote models fold into one
    line, and if it still doesn't fit the middle is cut with a "… N more lines" line, the QUEUE
    kept: cut at the bottom, the queue went first, with nothing said (#608)."""
    out = _lines(snap)
    if rows is None or len(out) <= rows:
        return out
    folded = [r for r in snap["rack"] if r["state"] in FOLD and not r.get("tasks")]
    if folded:
        kept = [r for r in snap["rack"] if r not in folded]
        out = _lines({**snap, "rack": kept}, fold=len(folded))
    if len(out) <= rows:
        return out
    q = max(i for i, segs in enumerate(out) if segs == [("head", "QUEUE")]) - 1  # its blank line
    head, tail = out[:q], out[q:]  # the header, the GPUs, RUNNING and RACK; then the QUEUE
    room = rows - len(tail) - 1
    if room < 1:  # not even the queue fits: the top of it, and how much is hidden
        return out[:rows - 1] + [[("dim", f"  … {len(out) - rows + 1} more lines")]]
    return head[:room] + [[("dim", f"  … {len(head) - room} more lines")]] + tail


def _lines(snap: dict, fold: int = 0) -> list[list[tuple[str, str]]]:
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
        if g.get("limit"):  # an Apple GPU: the most of the RAM it may wire (#701)
            row += [("dim", f"GPU may wire {_gb(g['limit'])} GB  ")]
        if g.get("stale"):
            row += [("dim", "no reading  ")]
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
        if r.get("via") == "api":  # its provider and its spend (#1022): no GPU of ours
            row.append(("", f"  API {p(r.get('provider') or '?')}"
                        + (f" · {money(r['spend'], r.get('prices', meter.CATALOG))}"
                           if isinstance(r.get("spend"), (int, float)) else "")
                        + (f" · waits for {p(r['waits'])}" if r.get("waits") else "")))
        out.append(row)
        if r.get("last"):
            out.append([("dim", f"    {p(r['last'])}")])
    if not snap["running"]:
        out.append([("dim", "  nothing running")])
    out += [[], [("head", "RACK")]]
    for r in snap["rack"]:
        state = r["state"]
        style = {"resident": "ok", "cpu": "ok", "split": "warn", "can't run": "bad"}.get(state, "dim")
        where = ""
        if state == "cpu":
            where = f" {_gb(r['cpu_bytes'])} GB in RAM"
        elif r.get("gpu_bytes") is not None:
            where = f" {_gb(r['gpu_bytes'])} GB GPU" + (f" + {_gb(r['cpu_bytes'])} GB CPU" if r.get("cpu_bytes") else "")
        row = [("name", f"  {p(r['label'])[:30].ljust(31)}"), (style, f"{state}{where}".ljust(31))]
        facts = [f"writes ~{r['write_tok_s']:.0f}" if r.get("write_tok_s") else "",
                 f"reads ~{r['read_tok_s']}" if r.get("read_tok_s") else "",
                 f"loads ~{_secs(r['load_s'])}" if r.get("load_s") else ""]
        row.append(("dim", " · ".join(f for f in facts if f)))
        if isinstance((r.get("server") or {}).get("tok_s"), (int, float)):
            row.append(("", f"  server {r['server']['tok_s']:.0f} tok/s live"))
        if r.get("tasks"):
            row.append(("", "  ← " + ", ".join(p(t) for t in r["tasks"])))
        if r.get("why") and state != "remote":
            row.append(("dim", f"  {p(r['why'])}"))
        elif state == "remote":
            row.append(("dim", "  another machine: no telemetry"))
        out.append(row)
    if fold:
        out.append([("dim", f"  +{fold} idle, can't run or remote (a taller terminal shows them)")])
    elif not snap["rack"]:
        out.append([("dim", "  no models found")])
    out += [[], [("head", "QUEUE")]]
    for q in snap["queue"]:
        out.append([("", f"  {p(q['task'])}  {p(q.get('model') or '')}"),
                    ("dim", f"  waiting {_secs(q['waited_s'])}" if q.get("waited_s") is not None else "")])
    if not snap["queue"]:
        out.append([("dim", "  nobody waiting")])
    return out


def render(snap: dict, width: int, color: bool, height: int | None = None) -> str:
    """The lines cut to ``width`` columns (and fitted to ``height`` rows: `lines`), coloured on a
    terminal."""
    rows = []
    for segs in lines(snap, height):
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
    # the loop asks the servers on a thread; the one pass (not a terminal, no --json) waits for them
    top = Top(every, drm=drm, background=as_json or tty)
    keys = None
    saved = None
    wake = None  # a pipe SIGCONT writes to: back from Ctrl-Z, the view redraws at once

    def put(seq: str, raw: bool) -> None:
        """An escape for the terminal; from a signal handler (``raw``) straight to its fd. The
        handler can run inside out's own write, a frame waiting on a terminal that isn't reading
        (Ctrl-S, a slow link, a Mac's small pty buffer), where out.write raised "reentrant call
        inside <_io.BufferedWriter>" and Ctrl-Z quit wb top (#950, 2026-11-21)."""
        if not raw:
            out.write(seq)
            out.flush()
            return
        try:
            data = seq.encode()
            while data:
                data = data[os.write(out.fileno(), data):]
        except (OSError, ValueError):  # raised in a handler, it would end wb top wherever it landed
            pass

    def enter(raw: bool = False):  # cbreak for `q`, the alternate screen, the cursor hidden
        if saved is not None:
            import tty as tty_mod
            try:
                tty_mod.setcbreak(keys)
            except OSError:
                pass
        put("\033[?1049h\033[?25l", raw)

    def leave(raw: bool = False):  # the cursor, the main screen and the terminal's own mode back
        try:
            put("\033[?25h\033[?1049l", raw)
        except (BrokenPipeError, OSError):
            pass
        if saved is not None:
            import termios
            try:
                termios.tcsetattr(keys, termios.TCSADRAIN, saved)
            except OSError:
                pass

    # Ctrl-Z: put the terminal back before stopping, and take it again on SIGCONT. It stopped with
    # the shell's prompt drawn over the frozen view, no cursor, and after `fg` `q` needed Enter (#607)
    def suspend(_signum, _frame):
        leave(raw=True)
        signal.signal(signal.SIGTSTP, signal.SIG_DFL)
        os.kill(os.getpid(), signal.SIGTSTP)  # stops here until SIGCONT (an orphaned group: not)
        signal.signal(signal.SIGTSTP, suspend)
        resume(None, None)

    def resume(_signum, _frame):
        enter(raw=True)
        try:
            os.write(wake[1], b"!")
        except (OSError, TypeError):
            pass
    try:
        if tty:
            try:
                import termios
                if sys.stdin.isatty():
                    keys = sys.stdin.fileno()
                    saved = termios.tcgetattr(keys)
            except (ImportError, OSError):
                keys = saved = None
            wake = os.pipe()
            os.set_blocking(wake[0], False)
            os.set_blocking(wake[1], False)
            old.update({s: signal.signal(s, h) for s, h in
                        ((signal.SIGTSTP, suspend), (signal.SIGCONT, resume))})
            enter()
        while True:
            started = time.monotonic()
            snap = top.tick()
            if as_json:
                out.write(json.dumps(snap, ensure_ascii=False) + "\n")
                out.flush()
            elif tty:
                size = shutil.get_terminal_size()
                text = render(snap, size.columns, color=True, height=size.lines).split("\n")[:size.lines]
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
                fds = [f for f in (smi, keys, wake and wake[0]) if f is not None]
                if not fds:
                    time.sleep(left)
                    break
                ready = select.select(fds, [], [], left)[0]
                if smi is not None and smi in ready:
                    top.smi.drain()
                if wake and wake[0] in ready:  # back from a stop: redraw now
                    try:
                        os.read(wake[0], 64)
                    except BlockingIOError:
                        pass
                    break
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
        for s, h in old.items():
            signal.signal(s, h)
        if tty:
            leave()
        for fd in wake or ():
            os.close(fd)
