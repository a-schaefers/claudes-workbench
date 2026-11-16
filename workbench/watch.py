"""Waiting for things to happen without polling: file changes (inotify) and process exits (pidfd).

Every part of workbench talks through files (store.py), so "something happened" is always "a
file changed": a runner wrote status.json, Claude answered a question, the user sent a chat
message. A Watcher sleeps in the kernel until one of its directories changes and wakes at once.
Without inotify (macOS, or past the system's watch limit) it looks at those directories a few
times a second instead and reports what changed, so nothing here is required for workbench to work.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import os
import select
import struct
import time
from pathlib import Path

IN_MODIFY, IN_ATTRIB, IN_CLOSE_WRITE = 0x002, 0x004, 0x008
IN_MOVED_FROM, IN_MOVED_TO, IN_CREATE, IN_DELETE = 0x040, 0x080, 0x100, 0x200
IN_ISDIR, IN_NONBLOCK, IN_CLOEXEC = 0x40000000, 0o4000, 0o2000000
MASK = IN_MODIFY | IN_CLOSE_WRITE | IN_MOVED_FROM | IN_MOVED_TO | IN_CREATE | IN_DELETE
_EVENT = struct.Struct("iIII")
POLL_S = 0.25  # without inotify: how often a directory is looked at


def _scan(d: Path) -> dict:
    """A directory's entries as name -> (inode, mtime, size, is a directory)."""
    snap = {}
    try:
        with os.scandir(d) as it:
            for e in it:
                try:
                    st = e.stat(follow_symlinks=False)
                    snap[e.name] = (st.st_ino, st.st_mtime_ns, st.st_size, e.is_dir(follow_symlinks=False))
                except OSError:
                    continue
    except OSError:
        pass
    return snap

_libc = None


def _lib():
    global _libc
    if _libc is None:
        try:
            lib = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
            lib.inotify_init1.argtypes = [ctypes.c_int]
            lib.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
            _libc = lib
        except (OSError, AttributeError):
            _libc = False
    return _libc


class Watcher:
    """Watch directories (and, with recursive=True, the directories made inside them later).

    wait(timeout) returns the changed paths, or [] on timeout. fileno() lets a caller select()
    on it beside other descriptors."""

    def __init__(self, *dirs: Path, recursive: bool = False, skip: tuple[str, ...] = ()):
        self.recursive, self.skip = recursive, set(skip)
        self.dirs: dict[int, Path] = {}
        self.polled: dict[Path, dict] = {}  # directories inotify isn't watching: looked at instead
        lib = _lib()
        self.fd = lib.inotify_init1(IN_NONBLOCK | IN_CLOEXEC) if lib else -1
        for d in dirs:
            self.add(Path(d))

    @property
    def native(self) -> bool:
        return self.fd >= 0

    def fileno(self) -> int:
        return self.fd

    def add(self, d: Path) -> None:
        if not d.is_dir() or d.name in self.skip:
            return
        wd = _lib().inotify_add_watch(self.fd, os.fsencode(d), MASK) if self.native else -1
        if wd >= 0:
            self.dirs[wd] = d
        elif d not in self.polled:
            self.polled[d] = _scan(d)
        if self.recursive:
            for sub in d.iterdir():
                if sub.is_dir() and sub.name not in self.skip and sub not in self.dirs.values() \
                        and sub not in self.polled:
                    self.add(sub)

    def _poll(self) -> list[Path]:
        """What changed in the polled directories since they were last looked at."""
        changed = []
        for d, old in list(self.polled.items()):
            new = _scan(d)
            for name in old.keys() | new.keys():
                if old.get(name) != new.get(name):
                    changed.append(d / name)
                    if self.recursive and name not in old and new[name][3]:
                        self.add(d / name)
            if new or d.exists():
                self.polled[d] = new
            else:
                del self.polled[d]
        return changed

    def read(self) -> list[Path]:
        """The changes waiting now (non-blocking)."""
        changed = []
        while True:
            try:
                buf = os.read(self.fd, 64 * 1024)
            except BlockingIOError:
                return changed
            i = 0
            while i < len(buf):
                wd, mask, _, size = _EVENT.unpack_from(buf, i)
                name = buf[i + _EVENT.size:i + _EVENT.size + size].rstrip(b"\0")
                i += _EVENT.size + size
                base = self.dirs.get(wd)
                if base is None:
                    continue
                path = base / os.fsdecode(name) if name else base
                if (self.recursive and mask & IN_ISDIR and mask & (IN_CREATE | IN_MOVED_TO)
                        and path.name not in self.skip):
                    self.add(path)
                changed.append(path)

    def wait(self, timeout: float | None = None, settle: float = 0.02) -> list[Path]:
        """Block until something changes (then gather the burst for `settle` seconds), or until
        the timeout. Without inotify it looks every POLL_S and returns within a second, so
        callers also re-check what no file says."""
        if self.native and not self.polled:
            ready, _, _ = select.select([self.fd], [], [], timeout)
            if not ready:
                return []
            changed = self.read()
            if settle:
                time.sleep(settle)
                changed += self.read()
            return changed
        cap = timeout if timeout is not None else float("inf")
        deadline = time.monotonic() + (cap if self.native else min(cap, 1.0))
        while True:
            tick = max(0.0, min(POLL_S, deadline - time.monotonic()))
            if self.native:
                ready, _, _ = select.select([self.fd], [], [], tick)
                changed = self.read() if ready else []
            else:
                time.sleep(tick)
                changed = []
            changed += self._poll()
            if changed:
                if settle:
                    time.sleep(settle)
                    changed += (self.read() if self.native else []) + self._poll()
                return changed
            if time.monotonic() >= deadline:
                return []

    def close(self) -> None:
        if self.native:
            os.close(self.fd)
            self.fd = -1

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def wait_exit(pid: int, timeout: float) -> bool:
    """Wait for a process to end, woken by the kernel the moment it does. True if it ended."""
    try:
        fd = os.pidfd_open(pid)
    except ProcessLookupError:
        return True
    except (AttributeError, OSError):  # no pidfd: check a few times a second
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            time.sleep(0.1)
        return False
    try:
        return bool(select.select([fd], [], [], timeout)[0])
    finally:
        os.close(fd)
