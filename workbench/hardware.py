"""What this machine has to run models on, found fresh each time: nothing about it is written down.

`wb scout` reports it next to the installed models, so Claude can judge each week which models
are the best fit for this hardware right now (the /delegate skill's weekly model check).

Linux reads sysfs and /proc; WSL2 adds what Windows says about its GPUs (only NVIDIA's are
visible inside WSL); macOS asks sysctl and system_profiler, and on Apple Silicon the GPU's memory
is the unified RAM. Anything a platform can't tell comes back as unknown, never as a guess.
"""
from __future__ import annotations

import json
import os
import platform
import re
import shlex
import shutil
import subprocess
from pathlib import Path

GB = 1024 ** 3


def _read(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def _run(*argv: str) -> str:
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def system() -> str:
    """linux, wsl, macos, or what Python calls the platform."""
    name = platform.system()
    if name == "Darwin":
        return "macos"
    if name == "Linux":
        return "wsl" if "microsoft" in _read(Path("/proc/version")).lower() else "linux"
    return name.lower() or "unknown"


def _sysctl(key: str) -> str:
    return _run("sysctl", "-n", key)


def _mac_gpus() -> list[dict]:
    """system_profiler's display adapters. Apple Silicon has no VRAM of its own: the GPU uses the
    unified RAM, of which macOS lets it wire only part by default (iogpu.wired_limit_mb raises it)."""
    try:
        items = json.loads(_run("system_profiler", "SPDisplaysDataType", "-json") or "{}")
    except ValueError:
        items = {}
    found = []
    for g in items.get("SPDisplaysDataType", []):
        name = g.get("sppci_model") or g.get("_name") or "unknown"
        if g.get("sppci_cores"):
            name += f" ({g['sppci_cores']}-core GPU)"
        vram = g.get("spdisplays_vram") or g.get("spdisplays_vram_shared") or ""
        m = re.match(r"(\d+)\s*(MB|GB)", vram)
        unified = name.startswith("Apple")
        shared = not g.get("spdisplays_vram") and not unified  # an Intel iGPU's slice of RAM
        found.append({"name": name, "driver": "metal", "unified": unified,
                      "vram": (_int(_sysctl("hw.memsize")) if unified else None if shared else
                               int(m.group(1)) * (GB if m.group(2) == "GB" else 2 ** 20) if m else None),
                      "vram_used": None,
                      "wired_limit": _int(_sysctl("iogpu.wired_limit_mb")) * 2 ** 20
                      if unified and _int(_sysctl("iogpu.wired_limit_mb")) else None})
    return found


def _windows_gpus() -> list[dict]:
    """From inside WSL2: the Windows host's adapters by name (WSL hides all but NVIDIA's from
    Linux). Windows' AdapterRAM is capped at 4 GB, so VRAM stays unknown rather than wrong."""
    out = _run("powershell.exe", "-NoProfile", "-Command",
               "Get-CimInstance Win32_VideoController | Select-Object -ExpandProperty Name")
    return [{"name": n.strip(), "driver": "windows (seen from WSL)", "vram": None, "vram_used": None}
            for n in out.splitlines() if n.strip()]


def _int(text: str) -> int:
    try:
        return int(text.strip())
    except (ValueError, AttributeError):
        return 0


def gpus() -> list[dict]:
    """Every display adapter: name, driver, VRAM total and in use (bytes) where the driver says."""
    where = system()
    if where == "macos":
        return _mac_gpus()
    found = []
    smi = _run("nvidia-smi", "--query-gpu=name,memory.total,memory.used,driver_version",
               "--format=csv,noheader,nounits").splitlines()
    for line in smi:
        parts = [x.strip() for x in line.split(",")]
        if len(parts) != 4:
            continue
        name, total, used, driver = parts
        mib = lambda v: int(v) * 2 ** 20 if v.isdigit() else None  # noqa: E731 ("[N/A]" on some cards)
        found.append({"name": name, "driver": f"nvidia {driver}", "vram": mib(total),
                      "vram_used": mib(used)})
    if where == "wsl":
        nvidia = {g["name"] for g in found}
        return found + [g for g in _windows_gpus()
                        if not any(n in g["name"] or g["name"] in n for n in nvidia)]
    seen = set()
    for dev in sorted(Path("/sys/class/drm").glob("card[0-9]*/device")):
        if "-" in dev.parent.name:  # a connector (card0-DP-1), not a card
            continue
        uevent = dict(x.split("=", 1) for x in _read(dev / "uevent").splitlines() if "=" in x)
        slot, driver = uevent.get("PCI_SLOT_NAME"), uevent.get("DRIVER", "")
        if not slot or slot in seen or (driver == "nvidia" and smi):  # nvidia-smi spoke for those
            continue
        seen.add(slot)
        name = _run("lspci", "-s", slot).partition(": ")[2] or uevent.get("PCI_ID", "unknown")
        total, used = _read(dev / "mem_info_vram_total"), _read(dev / "mem_info_vram_used")
        found.append({"name": name, "driver": driver, "vram": int(total) if total else None,
                      "vram_used": int(used) if used else None})
    return found


def memory() -> dict:
    if system() == "macos":
        page = _int(_sysctl("hw.pagesize")) or 4096
        stats = dict(re.findall(r"^(Pages [\w ]+):\s+(\d+)\.", _run("vm_stat"), re.M))
        free = sum(_int(stats.get(k, "0")) for k in ("Pages free", "Pages inactive", "Pages speculative"))
        swap = re.findall(r"(total|free) = ([\d.]+)M", _sysctl("vm.swapusage"))
        sw = {k: int(float(v) * 2 ** 20) for k, v in swap}
        return {"ram": _int(_sysctl("hw.memsize")) or None, "ram_free": free * page or None,
                "swap": sw.get("total"), "swap_free": sw.get("free")}
    info = {}
    for line in _read(Path("/proc/meminfo")).splitlines():
        key, _, rest = line.partition(":")
        if rest.strip().endswith("kB"):
            info[key] = int(rest.split()[0]) * 1024
    return {"ram": info.get("MemTotal"), "ram_free": info.get("MemAvailable"),
            "swap": info.get("SwapTotal"), "swap_free": info.get("SwapFree")}


def cpu() -> dict:
    name = _sysctl("machdep.cpu.brand_string") if system() == "macos" else next(
        (line.partition(":")[2].strip() for line in _read(Path("/proc/cpuinfo")).splitlines()
         if line.startswith("model name")), "")
    name = name or platform.machine() or "unknown"
    return {"name": name, "threads": os.cpu_count()}


OLLAMA_KEYS = ("OLLAMA_MODELS", "OLLAMA_CONTEXT_LENGTH", "OLLAMA_NUM_PARALLEL", "OLLAMA_HOST")


def ollama_env() -> dict:
    """The Ollama service's environment (where it keeps models, its default context, where it
    listens): systemd's unit (the system's, else the user's), or on macOS what the app reads
    (launchctl setenv). An Ollama started by hand reads the caller's environment instead."""
    env = {}
    if system() == "macos":
        env = {k: v for k in OLLAMA_KEYS if (v := _run("launchctl", "getenv", k))}
    else:
        for scope in ((), ("--user",)):
            raw = _run("systemctl", *scope, "show", "ollama", "-p", "Environment", "--value")
            try:  # systemd quotes an entry holding a space: "OLLAMA_MODELS=/data/my models"
                items = shlex.split(raw)  # (split on spaces, it was lost: the 2026-11-16 audit)
            except ValueError:  # an unbalanced quote
                items = raw.split()
            for item in items:
                k, _, v = item.partition("=")
                env.setdefault(k, v)
    for k in OLLAMA_KEYS:
        env.setdefault(k, os.environ.get(k, ""))
    return env


def ollama_context() -> tuple[int | None, str]:
    """OLLAMA_CONTEXT_LENGTH as Ollama reads it (spaces and quotes trimmed), and the raw value.
    wb scout crashed on "32k", which Ollama only warns about (the 2026-11-16 audit)."""
    raw = ollama_env().get("OLLAMA_CONTEXT_LENGTH") or ""
    v = raw.strip().strip("\"'")
    return (int(v) if v.isdigit() and int(v) > 0 else None), raw


def ollama_models_dir() -> Path | None:
    candidates = [ollama_env().get("OLLAMA_MODELS"), str(Path.home() / ".ollama" / "models"),
                  "/usr/share/ollama/.ollama/models", "/var/lib/ollama"]
    # os.path.isdir, not Path.is_dir: before 3.14 that raises on a directory it can't read, such
    # as Ubuntu's 0750 /usr/share/ollama, and wb scout died (the 2026-11-16 audit)
    return next((Path(c) for c in candidates if c and os.path.isdir(c)), None)


def disk_free(path: Path | None) -> int | None:
    try:
        return shutil.disk_usage(path).free if path else None
    except OSError:
        return None


def size(n: int | None) -> str:
    return f"{n / GB:.1f} GB" if n else "?"


SYSTEM_NOTE = {
    "wsl": "WSL2: RAM is the VM's share (.wslconfig sets it); only NVIDIA GPUs are usable from "
           "inside WSL, others are named from Windows without their VRAM",
    "macos": "macOS: Apple Silicon models run on the GPU through Metal, in the unified memory",
}


def report() -> list[str]:
    where = system()
    lines = [f"  OS    {where} ({platform.machine() or '?'})"
             + (f" · {SYSTEM_NOTE[where]}" if where in SYSTEM_NOTE else "")]
    gpu_lines = []
    for g in gpus():
        if g.get("unified"):
            limit = (f", the GPU may wire {size(g['wired_limit'])} (iogpu.wired_limit_mb)"
                     if g.get("wired_limit") else ", of which macOS lets the GPU use only part by default")
            vram = f" · unified memory: {size(g['vram'])} shared with the CPU{limit}"
        else:
            vram = (f" · {size(g['vram'])} VRAM" + (f" ({size(g['vram_used'])} in use)"
                                                    if g.get("vram_used") else "")) if g["vram"] \
                else " · VRAM unknown"
        gpu_lines.append(f"  GPU   {g['name']}{vram} · {g['driver']}")
    lines += gpu_lines or ["  GPU   none found (models would run on the CPU)"]
    m, c = memory(), cpu()
    lines.append(f"  RAM   {size(m['ram'])} ({size(m['ram_free'])} free)"
                 + (f" · swap {size(m['swap'])} ({size(m['swap_free'])} free)" if m.get("swap") else ""))
    lines.append(f"  CPU   {c['name']} · {c['threads']} threads")
    where = ollama_models_dir()
    if where:
        lines.append(f"  disk  {size(disk_free(where))} free where Ollama keeps models ({where})")
    ctx, raw = ollama_context()
    if ctx:
        lines.append(f"  ollama serves a {ctx // 1024}K context by default (OLLAMA_CONTEXT_LENGTH)")
    elif raw.strip():
        lines.append(f"  ollama: OLLAMA_CONTEXT_LENGTH={raw!r} isn't a number; Ollama ignores it "
                     "and serves its own default")
    return lines
