"""What this machine has to run models on, found fresh each time: nothing about it is written down.

`wb scout` reports it next to the installed models, so Claude can judge each week which models
are the best fit for this hardware right now (the /workbench skill's weekly model check).

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
import time
from pathlib import Path

GB = 1024 ** 3
DRM = Path("/sys/class/drm")


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


# Where WSL puts the Windows driver's nvidia-smi: on PATH in a login shell, not always under a
# systemd unit, where the GPU then went unseen (the 2026-11-19 audit)
WSL_SMI = "/usr/lib/wsl/lib/nvidia-smi"


def smi() -> str:
    """nvidia-smi from PATH, else WSL's."""
    return "nvidia-smi" if shutil.which("nvidia-smi") or not os.path.isfile(WSL_SMI) else WSL_SMI


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


def gpus(windows: bool = True) -> list[dict]:
    """Every display adapter: name, driver, VRAM total and in use (bytes) where the driver says,
    and `discrete` when it is known to be a card with memory of its own (not an APU's carve-out).
    ``windows`` False leaves out what WSL names from Windows (no VRAM, and a PowerShell start of
    seconds): the GPU budget and wb image's settle asked it on every call (the 2026-11-16 audit)."""
    where = system()
    if where == "macos":
        return _mac_gpus()
    found = []
    listed = _run(smi(), "--query-gpu=uuid,name,memory.total,memory.used,driver_version",
                  "--format=csv,noheader,nounits").splitlines()
    for line in listed:
        parts = [x.strip() for x in line.split(",")]
        if len(parts) < 5:
            continue
        uuid, name, (total, used, driver) = parts[0], ", ".join(parts[1:-3]), parts[-3:]
        mib = lambda v: int(v) * 2 ** 20 if v.isdigit() else None  # noqa: E731 ("[N/A]" on some cards)
        g = {"name": name, "driver": f"nvidia {driver}", "vram": mib(total),
             "vram_used": mib(used), "discrete": True, "uuid": uuid}
        if g["vram"] is None and platform.machine().lower() in ("aarch64", "arm64"):
            # DGX Spark, Jetson: the GPU shares the RAM and nvidia-smi gives it no size ([N/A]),
            # so there was no budget at all (the 2026-11-19 audit). An x86 card's [N/A] stays unknown
            g.update(vram=memory()["ram"], unified=True, discrete=False,
                     memory="the RAM: nvidia-smi gives the GPU no memory of its own")
        found.append(g)
    if where == "wsl":
        if not windows:
            return found
        nvidia = {g["name"] for g in found}
        return found + [g for g in _windows_gpus()
                        if not any(n in g["name"] or g["name"] in n for n in nvidia)]
    seen = set()
    for dev in sorted(DRM.glob("card[0-9]*/device")):
        if "-" in dev.parent.name:  # a connector (card0-DP-1), not a card
            continue
        uevent = dict(x.split("=", 1) for x in _read(dev / "uevent").splitlines() if "=" in x)
        slot, driver = uevent.get("PCI_SLOT_NAME"), uevent.get("DRIVER", "")
        if not slot or slot in seen or (driver == "nvidia" and listed):  # nvidia-smi spoke for those
            continue
        seen.add(slot)
        name = _run("lspci", "-s", slot).partition(": ")[2] or uevent.get("PCI_ID", "unknown")
        total, used = _read(dev / "mem_info_vram_total"), _read(dev / "mem_info_vram_used")
        # amdgpu reports an APU's carve-out of system RAM as VRAM too; only a card's own memory
        # has a vendor (mem_info_vram_vendor: samsung, hynix…), so that marks a discrete one.
        # Without it the device stays unknown, not integrated (models.budget_devices)
        g = {"name": name, "driver": driver, "vram": int(total) if total else None,
             "vram_used": int(used) if used else None,
             **({"discrete": True} if _read(dev / "mem_info_vram_vendor") else {})}
        gtt = _int(_read(dev / "mem_info_gtt_total"))
        if driver == "amdgpu" and not g.get("discrete") and gtt:
            # an APU's GPU also maps system RAM up to its GTT size, which is where a Strix Halo
            # with a 512 MiB carve-out keeps its models: counting the carve-out alone dropped it
            # as an iGPU, so no budget (the 2026-11-19 audit)
            g.update(vram=(g["vram"] or 0) + gtt, unified=True,
                     memory=f"{size(g['vram'])} carve-out + {size(gtt)} GTT")
        if uid := _read(dev / "unique_id"):
            g["uuid"] = f"GPU-{uid}"  # how ROCR_VISIBLE_DEVICES names it
        found.append(g)
    return found


# What a runtime started with these sees, by driver: CUDA's and ROCm's own settings, which take
# device numbers (counted in nvidia-smi's or sysfs's order) or UUIDs. HIP's counts within ROCR's,
# in the order ROCR's names them; a Vulkan-backed Ollama's mask counts every card ("*").
VISIBLE = (("nvidia", "CUDA_VISIBLE_DEVICES"), ("amdgpu", "ROCR_VISIBLE_DEVICES"),
           ("amdgpu", "HIP_VISIBLE_DEVICES"), ("*", "GGML_VK_VISIBLE_DEVICES"))
EMPTY_IS_NONE = {"CUDA_VISIBLE_DEVICES"}  # set but empty: CUDA sees no device at all


def visible(devs: list[dict], env: dict) -> tuple[list[dict], list[str]]:
    """The devices of ``devs`` a runtime started with ``env`` can use, in the order it numbers
    them, and the settings that left some out. Two 8 GiB cards with Ollama limited to one made a
    16 GiB budget (the 2026-11-19 audit). An unset setting leaves every device; like CUDA, the
    list stops at the first entry that names no device ("-1": none). Each setting counts within
    the order the one before left: HIP_VISIBLE_DEVICES=0 under ROCR_VISIBLE_DEVICES=1,0 is
    ROCR's card 1, where discovery order picked the 4 GB card for the 24 GB one; an empty
    CUDA_VISIBLE_DEVICES is no device, not every one; and a Vulkan-backed Ollama's own mask was
    never read (#255, the audit of 1.2)."""
    notes = []
    for driver, key in VISIBLE:
        if key not in env or env[key] is None:
            continue
        raw = env[key].strip()
        mine = [g for g in devs if driver == "*" or (g.get("driver") or "").split(" ")[0] == driver]
        if not mine or not raw and key not in EMPTY_IS_NONE:
            continue
        picked = []
        for item in (x.strip() for x in raw.split(",")) if raw else ():
            if item.isdigit() and int(item) < len(mine):
                one = [mine[int(item)]]
            else:  # a UUID, or the unique start of one, as CUDA allows
                one = [g for g in mine if item and (g.get("uuid") or "").lower().startswith(item.lower())]
            if len(one) != 1:
                break
            picked += [g for g in one if not any(g is p for p in picked)]
        if [id(g) for g in picked] != [id(g) for g in mine]:
            devs = [g for g in devs if not any(g is m for m in mine)] + picked
            if len(picked) < len(mine):
                notes.append(f"{key}={raw}")
    return devs, notes


_ENERGY: dict = {}  # hwmon energy1_input path → (monotonic seconds, microjoules) at the last reading


def gpu_power_w(drm: Path = Path("/sys/class/drm")) -> float | None:
    """Power draw of the discrete cards, in watts: nvidia-smi's power.draw, else each card's
    hwmon power1_average, power1_input (amdgpu generations with no average), or the average over
    the time since the last call from its energy1_input counter (Intel's i915 and xe publish only
    that): read from power1_average alone, those cards' energy stayed 0 and the savings said it
    wasn't measured (#271, the audit of 1.2). A counter's first reading, and one that went back
    (wrapped or reset), gives nothing for that interval."""
    if system() == "macos":
        return None
    total = 0.0
    nvidia = False
    for line in _run(smi(), "--query-gpu=power.draw", "--format=csv,noheader,nounits").splitlines():
        try:
            total += float(line.strip())
        except ValueError:
            continue  # "[N/A]" on cards with no driver
        nvidia = True  # at least one card reported a value
    if nvidia:
        return round(total, 1)
    found = False
    for mon in sorted(drm.glob("card[0-9]*/device/hwmon/hwmon*")):
        if "-" in mon.parent.parent.parent.name:  # a connector (card0-DP-1), not a card
            continue
        for name in ("power1_average", "power1_input"):
            try:
                total += int(_read(mon / name)) / 1e6
                found = True
                break
            except ValueError:
                continue  # missing, a card reporting nothing, or non-numeric text
        else:
            try:
                uj = int(_read(mon / "energy1_input"))
            except ValueError:
                continue
            now, key = time.monotonic(), str(mon / "energy1_input")
            last, _ENERGY[key] = _ENERGY.get(key), (now, uj)
            found = True
            if last and uj >= last[1] and now > last[0]:
                total += (uj - last[1]) / 1e6 / (now - last[0])
    return round(total, 1) if found and total else None


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


OLLAMA_KEYS = ("OLLAMA_MODELS", "OLLAMA_CONTEXT_LENGTH", "OLLAMA_NUM_PARALLEL",
               "OLLAMA_MAX_LOADED_MODELS", "OLLAMA_HOST")


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
    for _, k in VISIBLE:  # unset stays unset: an empty CUDA_VISIBLE_DEVICES is no device (#255)
        if k in os.environ:
            env.setdefault(k, os.environ[k])
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
        if g.get("memory"):  # a unified GPU off macOS: what it may use, and where that comes from
            vram = f" · unified memory: {size(g['vram'])} usable by the GPU ({g['memory']})"
        elif g.get("unified"):
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
