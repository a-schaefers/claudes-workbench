"""What this machine has to run models on, found fresh each time: nothing about it is written
down, but WSL's reading of Windows' adapters, which takes a PowerShell start (an hour's cache).

`wb scout` reports it next to the installed models, so Claude can judge each week which models
are the best fit for this hardware right now (the /workbench skill's weekly model check).

Linux reads sysfs and /proc; WSL2 adds what Windows says about its GPUs (a model served from
Windows runs on any of them; Linux inside WSL sees few); macOS asks sysctl and system_profiler,
and on Apple Silicon the GPU's memory is the unified RAM. Anything a platform can't tell comes back as unknown, never as a guess.
"""
from __future__ import annotations

import base64
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

from . import store

GIB = 1024 ** 3  # what system_profiler means by "GB"
DRM = Path("/sys/class/drm")


def _read(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def _run(*argv: str) -> str:
    # Not strict UTF-8: Windows PowerShell 5.1 writes a pipe in the console's OEM code page, and a
    # localized adapter name or error ('Adaptador de pantalla básico') raised UnicodeDecodeError
    # past main in wb doctor, scout, top and GPU admission, again on every call (#664)
    try:
        r = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=10)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


# Where WSL puts the Windows driver's nvidia-smi: on PATH in a login shell, not always under a
# systemd unit, where the GPU then went unseen (the 2026-11-19 audit)
WSL_SMI = "/usr/lib/wsl/lib/nvidia-smi"


def smi() -> str:
    """nvidia-smi from PATH, else WSL's."""
    return "nvidia-smi" if shutil.which("nvidia-smi") or not os.path.isfile(WSL_SMI) else WSL_SMI


def shares_ram() -> bool:
    """Whether an NVIDIA GPU nvidia-smi gives no memory ([N/A]) uses the RAM: on ARM (a DGX Spark,
    a Jetson) it does; on x86 a card's [N/A] stays unknown. gpus and wb top ask it alike (#572)."""
    return platform.machine().lower() in ("aarch64", "arm64")


TEGRA_RELEASE = Path("/etc/nv_tegra_release")
DEVICE_TREE = Path("/proc/device-tree")


def _tegra() -> str | None:
    """A Jetson's name (the device tree's model, else "NVIDIA Jetson") when this is one: L4T's
    /etc/nv_tegra_release, or "nvidia,tegra" in the device tree's compatible; else None."""
    compatible = _read(DEVICE_TREE / "compatible")
    if not _read(TEGRA_RELEASE) and "nvidia,tegra" not in compatible:
        return None
    return _read(DEVICE_TREE / "model").strip("\0").strip() or "NVIDIA Jetson"


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
    found, wired = [], None
    for g in items.get("SPDisplaysDataType", []):
        name = g.get("sppci_model") or g.get("_name") or "unknown"
        if g.get("sppci_cores"):
            name += f" ({g['sppci_cores']}-core GPU)"
        vram = g.get("spdisplays_vram") or g.get("spdisplays_vram_shared") or ""
        m = re.match(r"(\d+)\s*(MB|GB)", vram)
        unified = name.startswith("Apple")
        shared = not g.get("spdisplays_vram") and not unified  # an Intel iGPU's slice of RAM
        if unified and wired is None:
            wired = wired_limit()
        found.append({"name": name, "driver": "metal", "unified": unified,
                      "vram": (_int(_sysctl("hw.memsize")) if unified else None if shared else
                               int(m.group(1)) * (GIB if m.group(2) == "GB" else 2 ** 20) if m else None),
                      "vram_used": None,
                      "wired_limit": wired[0] if unified and wired[0] else None,
                      **({"wired_key": wired[1]} if unified and wired[0] else {})})
    return found


def wired_limit() -> tuple[int | None, str]:
    """The GPU's raised wired limit on Apple Silicon in bytes, and the sysctl that set it:
    iogpu.wired_limit_mb (macOS 14 on, in MiB), else Ventura's debug.iogpu.wired_limit (bytes);
    (None, "") when neither is set (0, or missing). Read from Sonoma's key alone, a limit raised on
    macOS 13 was ignored and the budget fell back to macOS's default share (#714)."""
    if mb := _int(_sysctl("iogpu.wired_limit_mb")):
        return mb * 2 ** 20, "iogpu.wired_limit_mb"
    if raw := _int(_sysctl("debug.iogpu.wired_limit")):
        return raw, "debug.iogpu.wired_limit"
    return None, ""


# Windows' display adapters, each with the memory its driver's registry key states
# (HardwareInformation.qwMemorySize, 64 bits: Win32_VideoController's AdapterRAM caps at 4 GB),
# one name and size a line, tab between. Sent as -EncodedCommand: no quoting through WSL's interop
WINDOWS_SCRIPT = (  # UTF-8 out: names arrive intact for the registry match (#664)
    "[Console]::OutputEncoding = [Text.Encoding]::UTF8; $r = @(Get-ItemProperty 'HKLM:\\SYSTEM\\CurrentControlSet\\Control\\Class\\"
    "{4d36e968-e325-11ce-bfc1-08002be10318}\\0*' -ErrorAction SilentlyContinue); "
    "Get-CimInstance Win32_VideoController | ForEach-Object { $n = $_.Name; "
    "$m = ($r | Where-Object { $_.DriverDesc -eq $n } | Select-Object -First 1).'HardwareInformation.qwMemorySize'; "
    "$n + [char]9 + $m }")
WINDOWS_CACHE = "wsl-gpus.json"  # in store.machine_dir() (#894): {ts, gpus: [{name, vram}], tried?}
WINDOWS_FOR = 3600  # seconds a reading serves: PowerShell takes seconds to start
WINDOWS_RETRY = 60  # seconds a failed reading waits before PowerShell is asked again
WSL_CONF = Path("/etc/wsl.conf")
# Windows' own PowerShell, under WSL's automount root: on PATH only while WSL appends Windows'
# PATH, which [interop] appendWindowsPath=false (a common tweak) turns off (#702)
WINDOWS_POWERSHELL = "c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"


def windows_program(name: str, rel: str) -> str | None:
    """A Windows program as seen from WSL: `name` from PATH, else `rel` (from the drive letter,
    c/...) under the automount root (/etc/wsl.conf's [automount] root, /mnt/ by default); None
    when neither has it. PATH has Windows' only while WSL appends it, which neither
    [interop] appendWindowsPath=false nor a systemd unit (the user manager's PATH) gets (#702,
    #920)."""
    if found := shutil.which(name):
        return found
    root, section = "/mnt/", ""
    for line in _read(WSL_CONF).splitlines():
        line = line.split("#", 1)[0].strip()
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().lower()
        elif section == "automount" and line.partition("=")[0].strip().lower() == "root":
            root = line.partition("=")[2].strip().strip("\"'") or root
    path = os.path.join(root.rstrip("/") + "/", rel)
    return path if os.path.isfile(path) else None


def powershell() -> str | None:
    """Windows' powershell.exe as seen from WSL (windows_program)."""
    return windows_program("powershell.exe", WINDOWS_POWERSHELL)


def _windows_gpus() -> list[dict]:
    """From inside WSL2: the Windows host's adapters, with their memory from the registry. A
    model served from Windows (Ollama for Windows) runs on any of them, and WSL shows Linux only
    NVIDIA's (and AMD's or Intel's to ROCm or Level Zero), so a 16 GB Radeon was "VRAM unknown"
    and had no budget (#522). Read at most once per WINDOWS_FOR, in WINDOWS_CACHE: the budget
    asks it on every admission when Linux sees no GPU. A read that found nothing (no PowerShell,
    one slower than _run's timeout after a boot: Windows always lists an adapter) is no reading:
    the last good one stays and PowerShell is asked again after WINDOWS_RETRY. Cached as an hour's
    "no GPU", a 16 GB Radeon had no budget, and a read that only sometimes worked flipped wb
    optimize's fingerprint, so each flip emptied the GPU to measure (#702)."""
    path = store.machine_dir() / WINDOWS_CACHE
    hit = store.read_json(path, None)
    hit = hit if isinstance(hit, dict) and isinstance(hit.get("gpus"), list) else {"gpus": []}
    now, rows = time.time(), hit["gpus"]
    fresh = rows and 0 <= now - (hit.get("ts") or 0) < WINDOWS_FOR
    if not fresh and not 0 <= now - (hit.get("tried") or 0) < WINDOWS_RETRY:
        exe = powershell()
        out = _run(exe, "-NoProfile", "-NonInteractive", "-EncodedCommand",
                   base64.b64encode(WINDOWS_SCRIPT.encode("utf-16-le")).decode()) if exe else ""
        read = []
        for line in out.splitlines():
            name, _, mem = (x.strip() for x in line.partition("\t"))
            if name:
                read.append({"name": name, "vram": int(mem) if mem.isdigit() and int(mem) else None})
        if read:
            rows, hit = read, {"ts": now, "gpus": read}
        else:
            hit = {**hit, "tried": now}  # the last good reading kept
        try:
            store.write_json(path, hit)
        except OSError:
            pass  # unwritable state: asked again next time
    return [{"name": r["name"], "driver": "windows (seen from WSL)", "vram": r.get("vram"), "vram_used": None}
            for r in rows if isinstance(r, dict) and isinstance(r.get("name"), str)]


def _int(text: str) -> int:
    try:
        return int(text.strip())
    except (ValueError, AttributeError):
        return 0


# Display-only adapters: a server's BMC VGA (ASPEED, Matrox), a VM's emulated card, a USB or
# firmware framebuffer. None runs models (no CUDA, ROCm, Level Zero or Vulkan device), so they are
# no GPU: counted as one, a CPU-only rack server was told to set gpu_gb for its BMC, an 8-GPU box
# was hinted to replace its budget with one number, and a Vulkan mask counted the BMC (#710)
DISPLAY_ONLY = frozenset({"ast", "mgag200", "bochs", "bochs-drm", "cirrus", "cirrus-qemu", "vmwgfx",
                          "qxl", "virtio_gpu", "virtio-gpu", "vboxvideo", "hyperv_drm", "simpledrm",
                          "udl", "evdi", "gud"})
# The drivers whose cards can run models, of those that may publish no memory size: only these
# get gpu_line's hint to set gpu_gb (#710)
COMPUTE = ("i915", "xe", "amdgpu", "nvidia", "nouveau", "windows", "metal")


def gpus(windows: bool = True) -> list[dict]:
    """Every display adapter that can run models (not DISPLAY_ONLY's): name, driver, VRAM total
    and in use (bytes) where the driver says,
    and `discrete` when it is known to be a card with memory of its own (not an APU's carve-out).
    On WSL, Windows' adapters too (_windows_gpus); ``windows`` False leaves them out whenever
    Linux sees a GPU of its own (a PowerShell start takes seconds: the GPU budget and wb image's
    settle asked it on every call, the 2026-11-16 audit), and takes them from the cache when it
    sees none (#522)."""
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
        if g["vram"] is None and shares_ram():
            # DGX Spark, Jetson: the GPU shares the RAM and nvidia-smi gives it no size ([N/A]),
            # so there was no budget at all (the 2026-11-19 audit). An x86 card's [N/A] stays unknown
            # (ram_free: what the system left, from which the budget takes its reserve, #524)
            ram = memory()
            g.update(vram=ram["ram"], ram_free=ram["ram_free"], unified=True, discrete=False,
                     memory="the RAM: nvidia-smi gives the GPU no memory of its own")
        found.append(g)
    if not found and where == "linux" and shares_ram() and (tegra := _tegra()):
        # JetPack 5 and earlier have no nvidia-smi, and the Tegra GPU is a platform device the
        # DRM scan skips: a Jetson read as no GPU, so no budget, no reserve (#524) and no fit
        # check on an 8 GB Orin Nano (#713). Its GPU is the RAM's, as nvidia-smi's [N/A] says
        ram = memory()
        if ram.get("ram"):
            found.append({"name": tegra, "driver": "nvidia (tegra)", "vram": ram["ram"],
                          "vram_used": None, "ram_free": ram["ram_free"], "unified": True,
                          "discrete": False, "memory": "the RAM: a Jetson's GPU shares it"})
    if len({(g["name"], g["vram"]) for g in found}) > 1:
        # cards that differ: CUDA numbers them fastest first by default, which visible() guesses
        # from this (a query of its own: a driver that lacks the field fails the whole query)
        caps = dict(tuple(x.strip() for x in line.split(",")) for line in _run(
            smi(), "--query-gpu=uuid,compute_cap", "--format=csv,noheader").splitlines()
            if line.count(",") == 1)
        for g in found:
            if re.fullmatch(r"\d+\.\d+", cap := caps.get(g["uuid"], "")):
                g["compute_cap"] = tuple(int(x) for x in cap.split("."))
    if where == "wsl":
        if not windows and found:
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
        if not slot or slot in seen or (driver == "nvidia" and listed) or driver in DISPLAY_ONLY:
            continue  # nvidia-smi spoke for those; a BMC's or a VM's display runs no model (#710)
        seen.add(slot)
        name = _run("lspci", "-s", slot).partition(": ")[2] or uevent.get("PCI_ID", "unknown")
        total, used = _read(dev / "mem_info_vram_total"), _read(dev / "mem_info_vram_used")
        # amdgpu reports an APU's carve-out of system RAM as VRAM too, so a device is discrete
        # only on evidence: a vendor for its memory (mem_info_vram_vendor: samsung, hynix…), or
        # an amdgpu hwmon that isn't an APU's (apu). Without either it stays unknown, not
        # integrated (models.budget_devices)
        kind = apu(dev) if driver == "amdgpu" else None
        g = {"name": name, "driver": driver, "vram": int(total) if total else None,
             "vram_used": int(used) if used else None,
             **({"discrete": True} if _read(dev / "mem_info_vram_vendor") or kind is False else {})}
        gtt = _int(_read(dev / "mem_info_gtt_total"))
        if kind and gtt:
            # an APU's GPU also maps system RAM up to its GTT size, which is where a Strix Halo
            # with a 512 MiB carve-out keeps its models: counting the carve-out alone dropped it
            # as an iGPU, so no budget (the 2026-11-19 audit)
            # The GTT can be raised toward all of RAM (amdgpu.gttsize, ttm.pages_limit), so the
            # RAM and what the system left are kept for the budget's reserve, as for a Jetson (#712)
            ram = memory()
            g.update(vram=(g["vram"] or 0) + gtt, unified=True, carve=g["vram"] or 0,
                     memory=f"{size(g['vram'])} carve-out + {size(gtt)} GTT",
                     **({"ram": ram["ram"], "ram_free": ram["ram_free"]}
                        if ram.get("ram") and ram.get("ram_free") is not None else {}))
        if uid := _read(dev / "unique_id"):
            g["uuid"] = f"GPU-{uid}"  # how ROCR_VISIBLE_DEVICES names it
        found.append(g)
    return found


# What a runtime started with these sees, by driver: CUDA's and ROCm's own settings, which take
# device numbers (counted in sysfs's order, or CUDA's: GUESSED) or UUIDs. HIP's counts within ROCR's,
# in the order ROCR's names them; a Vulkan-backed Ollama's mask counts every card ("*").
VISIBLE = (("nvidia", "CUDA_VISIBLE_DEVICES"), ("amdgpu", "ROCR_VISIBLE_DEVICES"),
           ("amdgpu", "HIP_VISIBLE_DEVICES"), ("*", "GGML_VK_VISIBLE_DEVICES"))
EMPTY_IS_NONE = {"CUDA_VISIBLE_DEVICES"}  # set but empty: CUDA sees no device at all
# How CUDA numbers its devices: nvidia-smi lists them in PCI order, CUDA by default fastest first
# (CUDA_DEVICE_ORDER=FASTEST_FIRST, a heuristic NVIDIA doesn't spell out), so on a 3090 beside a
# 1060 in a lower slot CUDA_VISIBLE_DEVICES=0 was the 1060 to wb and the 3090 to Ollama (#381,
# the audit of 1.6). PCI_BUS_ID makes the orders agree; a UUID needs no order at all.
GUESSED = (" (counted fastest first, CUDA's default order: a guess; CUDA_DEVICE_ORDER=PCI_BUS_ID "
           "or the card's UUID makes it sure)")


def visible(devs: list[dict], env: dict) -> tuple[list[dict], list[str]]:
    """The devices of ``devs`` a runtime started with ``env`` can use, in the order it numbers
    them, and the settings that left some out. Two 8 GiB cards with Ollama limited to one made a
    16 GiB budget (the 2026-11-19 audit). An unset setting leaves every device; like CUDA, the
    list stops at the first entry that names no device ("-1": none). Each setting counts within
    the order the one before left: HIP_VISIBLE_DEVICES=0 under ROCR_VISIBLE_DEVICES=1,0 is
    ROCR's card 1, where discovery order picked the 4 GB card for the 24 GB one; an empty
    CUDA_VISIBLE_DEVICES is no device, not every one; and a Vulkan-backed Ollama's own mask was
    never read (#255, the audit of 1.2). CUDA's numbers follow ``env``'s CUDA_DEVICE_ORDER:
    PCI_BUS_ID is nvidia-smi's order; else, when the cards differ, fastest first as near as
    can be told (compute capability, then memory), and the note says it is a guess (#381)."""
    notes = []
    for driver, key in VISIBLE:
        if key not in env or env[key] is None:
            continue
        raw = env[key].strip()
        mine = [g for g in devs if driver == "*" or (g.get("driver") or "").split(" ")[0] == driver]
        if not mine or not raw and key not in EMPTY_IS_NONE:
            continue
        order, guessed = mine, False
        if key == "CUDA_VISIBLE_DEVICES" and (env.get("CUDA_DEVICE_ORDER") or "").strip().upper() \
                != "PCI_BUS_ID" and len({(g.get("name"), g.get("vram")) for g in mine}) > 1:
            # fastest first, as near as can be told: compute capability, then memory (stable,
            # so PCI order among equals)
            order = sorted(mine, key=lambda g: (g.get("compute_cap") or (0,), g.get("vram") or 0), reverse=True)
        picked = []
        for item in (x.strip() for x in raw.split(",")) if raw else ():
            if item.isdigit() and int(item) < len(mine):
                one = [order[int(item)]]
                guessed |= order is not mine
            else:  # a UUID, or the unique start of one, as CUDA allows
                one = [g for g in mine if item and (g.get("uuid") or "").lower().startswith(item.lower())]
            if len(one) != 1:
                break
            picked += [g for g in one if not any(g is p for p in picked)]
        if [id(g) for g in picked] != [id(g) for g in mine]:
            devs = [g for g in devs if not any(g is m for m in mine)] + picked
            if len(picked) < len(mine):
                notes.append(f"{key}={raw}" + (GUESSED if guessed else ""))
    return devs, notes


_ENERGY: dict = {}  # hwmon energy1_input path → (monotonic seconds, microjoules) at the last reading


def _hwmon_watts(mon: Path) -> float | None:
    """One hwmon directory's power draw in watts: power1_average, else power1_input (amdgpu
    generations with no average), else the average since the last call from its energy1_input
    counter (Intel's i915 and xe publish only that); None when it reports none of them. A
    counter's first reading, and one that went back (wrapped or reset), gives 0 for that interval."""
    for name in ("power1_average", "power1_input"):
        try:
            return int(_read(mon / name)) / 1e6
        except ValueError:
            continue  # missing, a card reporting nothing, or non-numeric text
    try:
        uj = int(_read(mon / "energy1_input"))
    except ValueError:
        return None
    now, key = time.monotonic(), str(mon / "energy1_input")
    last, _ENERGY[key] = _ENERGY.get(key), (now, uj)
    if last and uj >= last[1] and now > last[0]:
        return (uj - last[1]) / 1e6 / (now - last[0])
    return 0.0


def gpu_power_w(drm: Path = DRM) -> float | None:
    """Power draw of the discrete cards, in watts: nvidia-smi's power.draw, else each card's
    hwmon (_hwmon_watts): read from power1_average alone, Intel's cards' energy stayed 0 and the
    savings said it wasn't measured (#271, the audit of 1.2). An APU's hwmon (apu) is the whole
    package's, its CPU cores too, so beside a card that isn't one it is left out: a Ryzen iGPU
    beside a Radeon counted the agent's builds and tests as GPU energy (#706)."""
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
    read = []  # (an APU's, watts) per card's hwmon
    for mon in sorted(drm.glob("card[0-9]*/device/hwmon/hwmon*")):
        if "-" in mon.parent.parent.parent.name:  # a connector (card0-DP-1), not a card
            continue
        w = _hwmon_watts(mon)
        if w is not None:
            read.append((apu(mon.parent.parent) is True, w))
    if any(not is_apu for is_apu, _ in read):
        read = [r for r in read if not r[0]]
    total = sum(w for _, w in read)
    return round(total, 1) if read and total else None


# ---- per-card readers for wb top: plain sysfs reads, cheap enough for every tick ----

def smi_path() -> str | None:
    """The nvidia-smi this machine has (PATH, else WSL's), or None: wb top streams from it."""
    return shutil.which("nvidia-smi") or (WSL_SMI if os.path.isfile(WSL_SMI) else None)


def drm_cards(drm: Path = DRM) -> list[dict]:
    """Each display card under ``drm`` once (by PCI slot; connectors skipped): ``card`` (card0),
    ``dev`` (its device directory), ``slot``, ``driver``, ``pci_id``. No lspci (card_name does
    that, once): wb top reads these every tick."""
    found, seen = [], set()
    for dev in sorted(drm.glob("card[0-9]*/device")):
        if "-" in dev.parent.name:  # a connector (card0-DP-1), not a card
            continue
        uevent = dict(x.split("=", 1) for x in _read(dev / "uevent").splitlines() if "=" in x)
        slot = uevent.get("PCI_SLOT_NAME") or dev.parent.name
        if slot in seen:
            continue
        seen.add(slot)
        found.append({"card": dev.parent.name, "dev": dev, "slot": slot,
                      "driver": uevent.get("DRIVER", ""), "pci_id": uevent.get("PCI_ID", "")})
    return found


def card_name(card: dict) -> str:
    """The card's name from lspci, else its PCI id: once per card, not per tick."""
    return (_run("lspci", "-s", card["slot"]).partition(": ")[2] if card.get("slot") else "") \
        or card.get("pci_id") or card.get("card") or "unknown"


def _num(text: str) -> int | None:
    return int(text) if text.isdigit() else None


def gpu_busy(dev: Path) -> int | None:
    """The card's busy percentage (amdgpu's and xe's gpu_busy_percent), None where it has none."""
    return _num(_read(dev / "gpu_busy_percent"))


def vram(dev: Path) -> tuple[int | None, int | None]:
    """(used, total) bytes of the card's VRAM from mem_info_vram_*, None each where not said."""
    return _num(_read(dev / "mem_info_vram_used")), _num(_read(dev / "mem_info_vram_total"))


def gtt(dev: Path) -> tuple[int | None, int | None]:
    """(used, total) bytes of the system RAM an amdgpu card maps (GTT): an APU's models live there."""
    return _num(_read(dev / "mem_info_gtt_used")), _num(_read(dev / "mem_info_gtt_total"))


def apu(dev: Path) -> bool | None:
    """Whether the amdgpu device at ``dev`` is an APU (its GPU on the CPU's die, its memory the
    system RAM): True on the kernel's word, False when it says otherwise, None when it can't say.
    The word is hwmon's vddnb (in1_label), the northbridge voltage amdgpu shows only on APUs
    (amdgpu_pm.c: "only APUs ... have vddnb"); an amdgpu hwmon without it is a card's. Taken
    from the lack of mem_info_vram_vendor, which the GCN cards' memory controllers never set,
    an 8 GiB RX 580 with 24 GiB of GTT was a 34.4 GB unified budget, in gpus and in wb top
    alike (#435). KFD's topology can't say: a GPU node never carries the CPU's cores."""
    mons = list(dev.glob("hwmon/hwmon*"))
    if any(_read(m / "in1_label") == "vddnb" for m in mons):
        return True
    return False if mons else None


def card_watts(dev: Path) -> float | None:
    """One card's power draw from its hwmon, in watts; None when it reports none."""
    total, found = 0.0, False
    for mon in sorted(dev.glob("hwmon/hwmon*")):
        w = _hwmon_watts(mon)
        if w is not None:
            found, total = True, total + w
    return round(total, 1) if found and total else None


def mac_free(page: int) -> int | None:
    """Bytes of a Mac's RAM free for use now: vm_stat's free, inactive and speculative pages of
    ``page`` bytes (hw.pagesize); None when vm_stat says nothing."""
    stats = dict(re.findall(r"^(Pages [\w ]+):\s+(\d+)\.", _run("vm_stat"), re.M))
    free = sum(_int(stats.get(k, "0")) for k in ("Pages free", "Pages inactive", "Pages speculative"))
    return free * page or None


def memory() -> dict:
    if system() == "macos":
        swap = re.findall(r"(total|free) = ([\d.]+)M", _sysctl("vm.swapusage"))
        sw = {k: int(float(v) * 2 ** 20) for k, v in swap}
        return {"ram": _int(_sysctl("hw.memsize")) or None,
                "ram_free": mac_free(_int(_sysctl("hw.pagesize")) or 4096),
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


OLLAMA_PORT = 11434  # Ollama's default, where an OLLAMA_HOST without a port listens
OLLAMA_KEYS = ("OLLAMA_MODELS", "OLLAMA_CONTEXT_LENGTH", "OLLAMA_NUM_PARALLEL",
               "OLLAMA_MAX_LOADED_MODELS", "OLLAMA_HOST")
# The server's own knobs, which mean the same read from a container: its OLLAMA_MODELS is a path
# inside it, its OLLAMA_HOST the container's bind, and its device masks count its own devices
OLLAMA_KNOBS = ("OLLAMA_CONTEXT_LENGTH", "OLLAMA_NUM_PARALLEL", "OLLAMA_MAX_LOADED_MODELS")
# unset stays unset: an empty CUDA_VISIBLE_DEVICES is no device (#255); CUDA_DEVICE_ORDER (#381)
SET_ONLY = (*(k for _, k in VISIBLE), "CUDA_DEVICE_ORDER")
PROC = Path("/proc")


def _environ(raw: bytes, keys) -> dict:
    """``keys`` from a NUL-separated environment block (/proc/<pid>/environ, docker's list)."""
    pairs = (x.decode(errors="replace").partition("=") for x in raw.split(b"\0") if b"=" in x)
    return {k: v for k, _, v in pairs if k in keys}


def _ollama_processes() -> list[tuple[dict, str, bool]]:
    """The environment of each `ollama serve` running here that this user may read
    (/proc/<pid>/environ: one started by hand in another terminal, a user unit's, anyone's as
    root), lowest pid first, what it is, and whether it runs in a container (another mount
    namespace: its knobs only). A system unit's process (user ollama) can't be read: systemd
    says its environment. macOS has no /proc: ps says (_ps_ollama)."""
    if system() == "macos":
        return _ps_ollama()
    try:
        pids = sorted(int(d.name) for d in PROC.iterdir() if d.name.isdigit())
    except OSError:
        return []
    found = []
    for pid in pids:
        d = PROC / str(pid)
        try:
            if (d / "comm").read_text().strip() != "ollama" \
                    or (d / "cmdline").read_bytes().split(b"\0")[1:2] != [b"serve"]:
                continue
            raw = (d / "environ").read_bytes()
        except (OSError, UnicodeDecodeError):
            continue  # gone, or another user's
        try:
            boxed = os.readlink(d / "ns" / "mnt") != os.readlink(PROC / "self" / "ns" / "mnt")
        except OSError:
            boxed = False
        if boxed:
            found.append((_environ(raw, OLLAMA_KNOBS),
                          f"the ollama process {pid} (in a container: its knobs only)", True))
        else:
            found.append((_environ(raw, OLLAMA_KEYS + SET_ONLY),
                          f"the running ollama's environment (pid {pid})", False))
    return found


def _ps_ollama() -> list[tuple[dict, str, bool]]:
    """_ollama_processes on macOS, from ps: each `ollama serve` (by hand, brew services, the app's)
    and the environment `ps -E` shows of this user's processes. With no /proc it found none, so
    OLLAMA_NUM_PARALLEL, OLLAMA_MAX_LOADED_MODELS and OLLAMA_CONTEXT_LENGTH were wb's own guesses:
    two tasks on one model were refused sharing (#804). A value with a space is cut at it (ps
    joins the environment with spaces); another user's process shows none and is left out."""
    found = []
    for line in _run("ps", "-axo", "pid=,args=").splitlines():
        pid, _, args = line.strip().partition(" ")
        if not pid.isdigit() or not re.match(r"(\S*/)?ollama serve(\s|$)", args.strip()):
            continue
        shown = _run("ps", "-wwE", "-o", "command=", "-p", pid)
        pairs = dict(re.findall(r"(?:^|\s)([A-Za-z_][A-Za-z0-9_]*)=(\S*)", shown))
        if not {"PATH", "HOME"} & set(pairs):
            continue  # no environment shown: not this user's to read
        found.append((int(pid), ({k: v for k, v in pairs.items() if k in OLLAMA_KEYS + SET_ONLY},
                                 f"the running ollama's environment (pid {pid})", False)))
    return [f for _, f in sorted(found, key=lambda f: f[0])]  # lowest pid first, as /proc's


def _launchd_ollama() -> dict | None:
    """A brew-services Ollama's environment as launchd holds it (its plist's
    EnvironmentVariables): `launchctl print gui/<uid>/homebrew.mxcl.ollama`. None when there is
    no such service."""
    out = _run("launchctl", "print", f"gui/{os.getuid()}/homebrew.mxcl.ollama")
    if not out:
        return None
    env, inside = {}, False
    for line in out.splitlines():
        t = line.strip()
        if t == "environment = {":
            inside = True
        elif inside and t == "}":
            break
        elif inside and " => " in t:
            k, _, v = t.partition(" => ")
            if k in OLLAMA_KEYS + SET_ONLY:
                env[k] = v
    return env


# What a systemd unit's EnvironmentFile= read could not, said in the settings' source: the
# settings may be missing, not Ollama's defaults (#804)
UNREAD = "unreadable"


def _environment_files(entries: list[str], keys) -> tuple[dict, list[str], list[str]]:
    """``keys`` from a unit's EnvironmentFile= files (systemctl show's `EnvironmentFiles=<path>
    (ignore_errors=yes|no)` lines), in order, a later one over an earlier; the files read and the
    ones that couldn't be (a missing file whose errors are ignored is neither: systemd skips it
    too). systemd's own format: KEY=VALUE lines, `#` and `;` comments, a value quoted whole. Left
    unread, /etc/default/ollama's CUDA_VISIBLE_DEVICES and OLLAMA_NUM_PARALLEL were taken as
    unset, and wb doctor said Ollama's defaults (#804)."""
    env, read, unread = {}, [], []
    for entry in entries:
        path, _, flags = entry.strip().partition(" (")
        optional = "ignore_errors=yes" in flags or path.startswith("-")
        path = path.lstrip("-")
        if not path:
            continue
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            if not optional:
                unread.append(path)
            continue
        except OSError:
            unread.append(path)
            continue
        read.append(path)
        for line in text.splitlines():
            t = line.strip()
            if not t or t[0] in "#;" or "=" not in t:
                continue
            k, _, v = t.partition("=")
            k, v = k.strip(), v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1].replace('\\"', '"') if v[0] == '"' else v[1:-1]
            if k in keys:
                env[k] = v
    return env, read, unread


def _port(host: str) -> int:
    """The port an OLLAMA_HOST value (``host:port``, ``:port``, a URL, or unset) listens on."""
    raw = (host or "").strip()
    try:
        return urlparse(raw if "://" in raw else f"http://{raw}").port or OLLAMA_PORT
    except ValueError:
        return OLLAMA_PORT


def ollama_env_at(root: str | None) -> dict:
    """The environment of the Ollama serving ``root``: with several `ollama serve` running here
    (one per GPU, on ports of their own), the one whose OLLAMA_HOST listens on root's port; else
    ollama_env(). The lowest pid's stood for every one: two cards with an Ollama each were
    budgeted as one, and the first one's NUM_PARALLEL and MAX_LOADED_MODELS read for both (#460,
    the audit of 1.8). One with no OLLAMA_HOST listens on OLLAMA_PORT: read as matching nothing,
    the default-port server started after the other got the other's settings (the review of
    #460). A container's process gives no OLLAMA_HOST of the host's, so it isn't matched."""
    return ollama_matched(root) or ollama_env()


def ollama_matched(root: str | None) -> dict | None:
    """ollama_env_at's environment when it is surely root's own: several `ollama serve` are
    readable here and exactly one listens on root's port. None otherwise (one process, none or
    two on that port, a container's), where ollama_env() is a guess that may be another
    server's: what can tell two servers' cards apart (models.server_devices) needs the sure one."""
    procs = _ollama_processes() if root else []
    if len(procs) > 1:
        want = urlparse(root).port
        hit = [env for env, _, boxed in procs if not boxed
               and _port(env.get("OLLAMA_HOST") or "") == want]
        if len(hit) == 1:
            env = dict(hit[0])
            for k in OLLAMA_KEYS:
                env.setdefault(k, "")
            return env
    return None


def _container_tools() -> tuple[str, ...]:
    """Whom to ask for an ollama container: docker (its daemon answers at once, or nothing does),
    and podman only where it has kept something for this user, rootless storage or macOS's podman
    machine (`~/.local/share/containers`, `$XDG_DATA_HOME`'s), or root's `/var/lib/containers`.
    Elsewhere it has no container to list, and a first `podman ps` builds its storage: seconds on a
    fresh CI runner, where a runner stopped meanwhile left it running past the test (2026-11-28)."""
    data = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
    own = data / "containers" if os.geteuid() else Path("/var/lib/containers")
    return ("docker", "podman") if own.is_dir() else ("docker",)


def _ollama_container() -> tuple[dict, str] | None:
    """A running container of an ollama image (`ollama/ollama`, any tag or registry), asked of
    docker or podman when it answers: its Config.Env, the knobs only."""
    for tool in _container_tools():
        for line in _run(tool, "ps", "--format", "{{.ID}} {{.Image}} {{.Names}}").splitlines():
            cid, image, name = (line.split() + ["", "", ""])[:3]
            if image.split("@")[0].rsplit("/", 1)[-1].split(":")[0] != "ollama":
                continue
            try:
                items = json.loads(_run(tool, "inspect", "--format", "{{json .Config.Env}}", cid) or "[]")
            except ValueError:
                items = []
            raw = b"\0".join(str(x).encode() for x in items if isinstance(x, str))
            try:
                host = json.loads(_run(tool, "inspect", "--format", "{{json .HostConfig}}", cid) or "{}")
            except ValueError:
                host = {}
            return ({**_environ(raw, OLLAMA_KNOBS), **container_mask(host, _environ(raw, SET_ONLY))},
                    f"the {tool} container {name or cid}")
    return None


def _within(granted: str | None, inner: str | None) -> str | None:
    """A container's own device mask ``inner`` in this host's numbering, its ordinals indexing
    the devices ``granted`` (the HostConfig's, None: all, numbered as here); a UUID is the same
    everywhere. Like CUDA, the list stops at the first entry that names no device."""
    if inner is None:
        return granted
    if granted is None:
        return inner
    cards, out = [x.strip() for x in granted.split(",") if x.strip()], []
    for item in (x.strip() for x in inner.split(",")) if inner.strip() else ():
        if item.isdigit() and int(item) < len(cards):
            out.append(cards[int(item)])
        elif item and not item.isdigit() and not item.startswith("-"):
            out.append(item)
        else:
            break
    return ",".join(out)


def container_mask(host, inner: dict | None = None) -> dict:
    """The devices a container's HostConfig gives it, as the masks visible() reads, in this
    host's numbering: `--gpus device=0` or compose `device_ids` (DeviceRequests' DeviceIDs, or a
    Count of the first N; NVIDIA's toolkit numbers them as nvidia-smi does, PCI order), and an AMD
    card passed as `--device /dev/dri/renderD129` (Devices). Unread, an Ollama limited to one card
    was budgeted every card (#463, the audit of 1.8). ``inner``: the container's own environment's
    masks, which number the devices it was given (_within): dropped, an Ollama granted two cards
    and set to CUDA_VISIBLE_DEVICES=0 was budgeted both (#651, Codex's audit of 1.90.6). {} when
    it has all and sets none."""
    inner = inner or {}
    if not isinstance(host, dict):
        host = {}
    out = {}
    for req in host.get("DeviceRequests") or []:
        if not isinstance(req, dict):
            continue
        caps = {c for group in req.get("Capabilities") or [] for c in group or []}
        if req.get("Driver") not in ("", None, "nvidia") or (caps and "gpu" not in caps):
            continue
        ids = [str(x) for x in req.get("DeviceIDs") or [] if str(x).strip()]
        count = req.get("Count") or 0
        if not ids and isinstance(count, int) and count > 0:
            ids = [str(i) for i in range(count)]
        if ids:
            out.update(CUDA_VISIBLE_DEVICES=",".join(ids), CUDA_DEVICE_ORDER="PCI_BUS_ID")
    renders = [str(d.get("PathOnHost") or "") for d in host.get("Devices") or [] if isinstance(d, dict)]
    renders = [p.rsplit("/", 1)[-1] for p in renders if re.fullmatch(r"/dev/dri/renderD\d+", p)]
    if renders:
        slot = lambda dev: dict(x.split("=", 1) for x in _read(dev / "uevent").splitlines()  # noqa: E731
                                if "=" in x).get("PCI_SLOT_NAME")
        amd = [c["slot"] for c in drm_cards(DRM) if c["driver"] == "amdgpu"]  # gpus()'s order
        picked = [str(amd.index(s)) for r in renders if (s := slot(DRM / r / "device")) in amd]
        if picked:
            out["ROCR_VISIBLE_DEVICES"] = ",".join(dict.fromkeys(picked))
    for key in ("CUDA_VISIBLE_DEVICES", "ROCR_VISIBLE_DEVICES"):
        if (got := _within(out.get(key), inner.get(key))) is not None:
            out[key] = got
    if "CUDA_VISIBLE_DEVICES" in inner and "CUDA_DEVICE_ORDER" not in out and inner.get("CUDA_DEVICE_ORDER"):
        out["CUDA_DEVICE_ORDER"] = inner["CUDA_DEVICE_ORDER"]  # all its cards: its own order
    if "HIP_VISIBLE_DEVICES" in inner:  # it numbers what ROCR leaves, inside as here
        out["HIP_VISIBLE_DEVICES"] = inner["HIP_VISIBLE_DEVICES"]
    return out


def ollama_settings() -> tuple[dict, str]:
    """The environment of the Ollama that runs here (where it keeps models, its default context,
    its sharing knobs, where it listens, the devices it may use), and where it was read; "" when
    nothing could say, and wb's own environment is the guess. In order: a running `ollama serve`
    this user may read; a running systemd unit (the system's, else the user's), its Environment
    split as a shell would (systemd quotes an entry holding a space: split on spaces it was lost,
    the 2026-11-16 audit), its EnvironmentFile= files over it (one it can't read said in the
    source: UNREAD, #804); an ollama container; on macOS (whose running `ollama serve` ps shows,
    #804) a brew service's plist (launchd), else what the app reads (launchctl setenv);
    a unit that isn't running, which Ollama reads when it starts. Read from systemd, launchctl or
    wb's own environment alone, a server in Docker, one started by hand, or Ollama for Windows
    from WSL had OLLAMA_NUM_PARALLEL and OLLAMA_MAX_LOADED_MODELS read as unset, so sharing was
    refused that fit or allowed where Ollama swaps (#379, the audit of 1.6). One source is the
    whole answer: wb's environment no longer fills in what a unit leaves unset."""
    first, idle = next(iter(_ollama_processes()), None), None
    found = first[:2] if first else None
    if first and first[2]:
        # a container's process gives its knobs, not the cards it was given: its HostConfig says
        # (container_mask). Read from the process alone (rootless docker or podman, whose
        # processes are this user's; wb as root), it was budgeted every card (the review of #463)
        found = _ollama_container() or found
    if not found and system() != "macos":
        for scope in ((), ("--user",)):
            raw = _run("systemctl", *scope, "show", "ollama", "-p", "LoadState", "-p", "ActiveState",
                       "-p", "Environment", "-p", "EnvironmentFiles")
            lines = raw.splitlines()
            props = dict(line.partition("=")[::2] for line in lines)
            if props.get("LoadState") != "loaded":
                continue
            try:
                items = shlex.split(props.get("Environment", ""))
            except ValueError:  # an unbalanced quote
                items = props.get("Environment", "").split()
            # EnvironmentFile= settings override Environment= ones (systemd.exec(5))
            files, read, unread = _environment_files(
                [line.partition("=")[2] for line in lines if line.startswith("EnvironmentFiles=")],
                OLLAMA_KEYS + SET_ONLY)
            name = "systemd's ollama unit" + (" (--user)" if scope else "") \
                + (f" and {', '.join(read)}" if read else "") \
                + (f"; {', '.join(unread)} {UNREAD}, so its settings may be missing" if unread else "")
            unit = ({**_environ("\0".join(items).encode(), OLLAMA_KEYS + SET_ONLY), **files}, name)
            if props.get("ActiveState") in ("active", "activating", "reloading"):
                found = unit
                break
            idle = idle or (unit[0], unit[1] + ", not running")
    found = found or _ollama_container()
    if not found and system() == "macos":
        if (env := _launchd_ollama()) is not None:  # brew services, not running now (#804)
            found = (env, "launchd's homebrew.mxcl.ollama, not running")
        elif env := {k: v for k in OLLAMA_KEYS if (v := _run("launchctl", "getenv", k))}:
            found = (env, "launchctl (what the Ollama app reads)")
    env, source = found or idle or ({k: os.environ[k] for k in OLLAMA_KEYS + SET_ONLY
                                     if k in os.environ}, "")
    for k in OLLAMA_KEYS:
        env.setdefault(k, "")
    return env, source


def ollama_env() -> dict:
    """The running Ollama's environment (ollama_settings)."""
    return ollama_settings()[0]


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
    """Bytes in decimal GB, the unit models.json's gpu_gb and vram_gb take (and Ollama prints):
    binary GiB labelled GB sat beside a decimal budget, and a hand-set gpu_gb came out 7% small
    (#523). 0 is 0.0 GB, only None unknown: a full disk and exhausted swap read "?" (#717)."""
    return f"{n / 1e9:.1f} GB" if n is not None else "?"


SYSTEM_NOTE = {
    "wsl": "WSL2: RAM is the VM's share (.wslconfig sets it); models served from Windows (Ollama "
           "for Windows) can use any vendor's GPU, and inside WSL NVIDIA (CUDA), recent AMD (ROCm "
           "for WSL) and Intel (Level Zero) work; Windows' adapters are read from Windows, their "
           "memory from its registry",
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
            limit = (f", the GPU may wire {size(g['wired_limit'])} ({g.get('wired_key') or 'iogpu.wired_limit_mb'})"
                     if g.get("wired_limit") else ", of which macOS lets the GPU use only part by default")
            vram = f" · unified memory: {size(g['vram'])} shared with the CPU{limit}"
        else:
            vram = (f" · {size(g['vram'])} VRAM" + (f" ({size(g['vram_used'])} in use)"
                                                    if g.get("vram_used") else "")) if g["vram"] \
                else " · VRAM unknown"
            if where == "macos":  # an Intel Mac's adapter: no budget counts it (#711)
                vram += " · Ollama runs models on the CPU on an Intel Mac (gpu_gb sets a budget by hand)"
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
