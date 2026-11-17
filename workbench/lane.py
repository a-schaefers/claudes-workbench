"""The agent writes only inside its lane, enforced by the kernel where it can be (Linux Landlock).

The runner starts OpenCode through this file (`python -I lane.py --write DIR... -- opencode ...`),
which restricts itself and then execs the command, so OpenCode, its shell and everything that runs
in it (make, an interpreter, a test suite, the MCP server) inherit the limit. Writes are handled,
and reading files when `--read` names what may be read (context.py: everything but the places
credentials live). Running programs, listing directories and the network stay as they were. A
write elsewhere fails with "Permission denied", and the agent asks Claude for the directory (`wb
allow --path`). context.py decides the lane; OpenCode's own `external_directory` rule keeps its
file tools to it as well.

Without Landlock (macOS, an old or locked-down kernel) it execs the command unrestricted and
`wb doctor` says so. A kernel that has Landlock but refuses the lane stops the command instead:
the agent never runs believed to be in a lane it isn't in. Only the standard library here: it runs before every round.
"""
from __future__ import annotations

import ctypes
import os
import platform
import signal
import struct
import sys

# syscall numbers, the same on the architectures with the unified table (added after it was);
# alpha's and MIPS's are offset, and there the lane is off rather than a wrong call made
CREATE_RULESET, ADD_RULE, RESTRICT_SELF = 444, 445, 446
UNIFIED = ("x86_64", "amd64", "i386", "i686", "aarch64", "arm64", "armv7l", "armv6l", "riscv64",
           "ppc64le", "ppc64", "s390x", "loongarch64")
RULE_PATH_BENEATH = 1
PR_SET_NO_NEW_PRIVS = 38
PR_SET_PDEATHSIG = 1
WRITE_FILE, READ_FILE, REMOVE_DIR, REMOVE_FILE = 1 << 1, 1 << 2, 1 << 4, 1 << 5
MAKE = sum(1 << b for b in range(6, 13))        # char, dir, reg, sock, fifo, block, sym
REFER, TRUNCATE = 1 << 13, 1 << 14               # ABI 2 and 3


def _libc():
    return ctypes.CDLL(None, use_errno=True)


def abi() -> int:
    """The kernel's Landlock version, 0 without it."""
    if not sys.platform.startswith("linux") or platform.machine().lower() not in UNIFIED:
        return 0
    try:
        v = _libc().syscall(CREATE_RULESET, None, ctypes.c_size_t(0), ctypes.c_uint32(1))
    except (OSError, AttributeError):
        return 0
    return max(v, 0)


def restrict(writable: list[str], readable: list[str] | None = None) -> int:
    """Forbid this process and its children every write outside `writable` (directories, and
    files such as /dev/null) and, given `readable`, reading a file outside it and `writable`.
    Returns the ABI used, 0 when Landlock isn't there."""
    version = abi()
    if not version:
        return 0
    libc = _libc()
    write = WRITE_FILE | REMOVE_DIR | REMOVE_FILE | MAKE
    if version >= 2:
        write |= REFER   # always denied unless handled: handled, a move inside the lane works
    if version >= 3:
        write |= TRUNCATE
    handled = write | (READ_FILE if readable is not None else 0)
    attr = ctypes.c_uint64(handled)
    fd = libc.syscall(CREATE_RULESET, ctypes.byref(attr), ctypes.c_size_t(8), ctypes.c_uint32(0))
    if fd < 0:
        raise OSError(ctypes.get_errno(), "landlock_create_ruleset")
    try:
        read = handled & READ_FILE
        for path, base in [*((p, write | read) for p in writable),
                           *((p, read) for p in readable or [])]:
            try:
                pfd = os.open(path, os.O_PATH | os.O_CLOEXEC)
            except OSError:
                continue  # gone since the lane was drawn
            try:
                rights = base if os.path.isdir(path) else base & (WRITE_FILE | READ_FILE | TRUNCATE)
                rule = ctypes.create_string_buffer(struct.pack("=Qi", rights, pfd))  # packed
                if libc.syscall(ADD_RULE, fd, RULE_PATH_BENEATH, rule,
                                ctypes.c_uint32(0)) < 0:
                    raise OSError(ctypes.get_errno(), f"landlock_add_rule {path}")
            finally:
                os.close(pfd)
        if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) < 0:
            raise OSError(ctypes.get_errno(), "prctl")
        if libc.syscall(RESTRICT_SELF, fd, ctypes.c_uint32(0)) < 0:
            raise OSError(ctypes.get_errno(), "landlock_restrict_self")
    finally:
        os.close(fd)
    return version


def wrap(writable: list[str] | None, cmd: list[str],
         readable: list[str] | None = None) -> list[str]:
    """The command that runs `cmd` inside the lane; `cmd` itself when there is no lane."""
    if writable is None:
        return cmd
    return [sys.executable, "-I", os.path.abspath(__file__),
            *(a for p in writable for a in ("--write", p)),
            *(["--reads"] if readable is not None else []),
            *(a for p in readable or [] for a in ("--read", p)), "--", *cmd]


def die_with_parent() -> None:
    """Linux: the kernel kills this process (and so the command it execs) when the runner that
    started it dies, even by SIGKILL, so an agent never works on with no runner to stop it (the
    2026-11-16 audit). Its own children are the runner's to sweep (runner.sweep)."""
    if not sys.platform.startswith("linux"):
        return
    parent = os.getppid()
    try:
        _libc().prctl(PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0)
    except (OSError, AttributeError):
        return
    if os.getppid() != parent:  # the runner died before the line above
        sys.exit(1)


def main(argv: list[str]) -> None:
    die_with_parent()
    sep = argv.index("--")
    writable = [argv[i + 1] for i in range(sep) if argv[i] == "--write"]
    readable = [argv[i + 1] for i in range(sep) if argv[i] == "--read"] \
        if "--reads" in argv[:sep] else None
    try:
        restrict(writable, readable)
    except OSError as e:  # Landlock is there but refused the lane: don't run outside it
        print(f"workbench lane: the kernel refused the lane ({e}); not running "
              f"{argv[sep + 1]}", file=sys.stderr)
        sys.exit(126)
    try:
        os.execvp(argv[sep + 1], argv[sep + 1:])
    except OSError as e:  # a traceback in runner.log, and the round only "exited 1" (the 2026-11-16 audit)
        print(f"workbench lane: can't run {argv[sep + 1]} ({e.strerror})", file=sys.stderr)
        sys.exit(127 if isinstance(e, FileNotFoundError) else 126)


if __name__ == "__main__":
    main(sys.argv[1:])
