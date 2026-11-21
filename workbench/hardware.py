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

GIB = 1024 ** 3  # what system_profiler means by "GB"
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
                               int(m.group(1)) * (GIB if m.group(2) == "GB" else 2 ** 20) if m else None),
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
            # (ram_free: what the system left, from which the budget takes its reserve, #524)
            ram = memory()
            g.update(vram=ram["ram"], ram_free=ram["ram_free"], unified=True, discrete=False,
                     memory="the RAM: nvidia-smi gives the GPU no memory of its own")
        found.append(g)
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
            g.update(vram=(g["vram"] or 0) + gtt, unified=True,
                     memory=f"{size(g['vram'])} carve-out + {size(gtt)} GTT")
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
    savings said it wasn't measured (#271, the audit of 1.2)."""
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
        w = _hwmon_watts(mon)
        if w is not None:
            found = True
            total += w
    return round(total, 1) if found and total else None


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


def _ollama_process() -> tuple[dict, str] | None:
    """The environment of an `ollama serve` running here that this user may read
    (/proc/<pid>/environ: one started by hand in another terminal, a user unit's, anyone's as
    root), and what it is. One in another mount namespace (a container) gives its knobs only.
    A system unit's process (user ollama) can't be read: systemd says its environment."""
    try:
        pids = sorted(int(d.name) for d in PROC.iterdir() if d.name.isdigit())
    except OSError:
        return None
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
            return _environ(raw, OLLAMA_KNOBS), f"the ollama process {pid} (in a container: its knobs only)"
        return _environ(raw, OLLAMA_KEYS + SET_ONLY), f"the running ollama's environment (pid {pid})"
    return None


def _ollama_container() -> tuple[dict, str] | None:
    """A running container of an ollama image (`ollama/ollama`, any tag or registry), asked of
    docker or podman when it answers: its Config.Env, the knobs only."""
    for tool in ("docker", "podman"):
        for line in _run(tool, "ps", "--format", "{{.ID}} {{.Image}} {{.Names}}").splitlines():
            cid, image, name = (line.split() + ["", "", ""])[:3]
            if image.split("@")[0].rsplit("/", 1)[-1].split(":")[0] != "ollama":
                continue
            try:
                items = json.loads(_run(tool, "inspect", "--format", "{{json .Config.Env}}", cid) or "[]")
            except ValueError:
                items = []
            raw = b"\0".join(str(x).encode() for x in items if isinstance(x, str))
            return _environ(raw, OLLAMA_KNOBS), f"the {tool} container {name or cid}"
    return None


def ollama_settings() -> tuple[dict, str]:
    """The environment of the Ollama that runs here (where it keeps models, its default context,
    its sharing knobs, where it listens, the devices it may use), and where it was read; "" when
    nothing could say, and wb's own environment is the guess. In order: a running `ollama serve`
    this user may read; a running systemd unit (the system's, else the user's), its Environment
    split as a shell would (systemd quotes an entry holding a space: split on spaces it was lost,
    the 2026-11-16 audit); an ollama container; on macOS what the app reads (launchctl setenv);
    a unit that isn't running, which Ollama reads when it starts. Read from systemd, launchctl or
    wb's own environment alone, a server in Docker, one started by hand, or Ollama for Windows
    from WSL had OLLAMA_NUM_PARALLEL and OLLAMA_MAX_LOADED_MODELS read as unset, so sharing was
    refused that fit or allowed where Ollama swaps (#379, the audit of 1.6). One source is the
    whole answer: wb's environment no longer fills in what a unit leaves unset."""
    found, idle = _ollama_process(), None
    if not found and system() != "macos":
        for scope in ((), ("--user",)):
            raw = _run("systemctl", *scope, "show", "ollama", "-p", "LoadState", "-p", "ActiveState",
                       "-p", "Environment")
            props = dict(line.partition("=")[::2] for line in raw.splitlines())
            if props.get("LoadState") != "loaded":
                continue
            try:
                items = shlex.split(props.get("Environment", ""))
            except ValueError:  # an unbalanced quote
                items = props.get("Environment", "").split()
            unit = (_environ("\0".join(items).encode(), OLLAMA_KEYS + SET_ONLY),
                    "systemd's ollama unit" + (" (--user)" if scope else ""))
            if props.get("ActiveState") in ("active", "activating", "reloading"):
                found = unit
                break
            idle = idle or (unit[0], unit[1] + ", not running")
    found = found or _ollama_container()
    if not found and system() == "macos":
        env = {k: v for k in OLLAMA_KEYS if (v := _run("launchctl", "getenv", k))}
        found = (env, "launchctl (what the Ollama app reads)") if env else None
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
    (#523)."""
    return f"{n / 1e9:.1f} GB" if n else "?"


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
