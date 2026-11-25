"""The agent writes only inside its lane, enforced by the kernel where it can be (Linux Landlock).

The runner starts OpenCode through this file (`python -I lane.py --write DIR... -- opencode ...`),
which restricts itself and then execs the command, so OpenCode, its shell and everything that runs
in it (make, an interpreter, a test suite, the MCP server) inherit the limit. Writes are handled,
and reading files when `--read` names what may be read (context.py: everything but the places
credentials live; running a program reads it, so it must be readable too). Where the kernel can
(ABI 9, ABI 6), it also refuses connecting to a UNIX socket made outside the lane (the session
bus, the SSH and GPG agents: `--unix` names the places that may), an abstract socket, or a signal
to a process outside it (the runner, other tasks, Claude Code). Listing directories and the network
stay as they were. Landlock doesn't cover metadata: chmod, chown, utimes and xattrs on a file
outside the lane still succeed. A write elsewhere fails with "Permission denied", and the agent
asks Claude for the directory (`wb allow --path`). context.py decides the lane; OpenCode's own
`external_directory` rule keeps its file tools to it as well.

No-new-privileges (no sudo, su or other setuid program) is set on every Linux machine, Landlock
or not. Without Landlock (macOS, an old or locked-down kernel) it otherwise execs the command
unrestricted and `wb doctor` says so. A kernel that has Landlock but refuses the lane stops the
command instead: the agent never runs believed to be in a lane it isn't in. Only the standard
library here: it runs before every round.
"""
from __future__ import annotations

import ctypes
import errno
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
RESOLVE_UNIX = 1 << 16                           # ABI 9: connect to a socket made outside
SCOPE_ABSTRACT_UNIX, SCOPE_SIGNAL = 1, 2         # ABI 6: ruleset_attr.scoped
# Where a rule's inode lasts only while something holds it (#1306, 2026-11-24): uncached 9p, WSL2's
# drvfs (/mnt/c), drops a dentry at its last close and gives the next lookup a new inode, which no
# rule is on. Landlock ties a rule to the inode, so once the fd it was added through closed, the
# repo's .git on /mnt/c was refused in an API task and every git call the agent made said "gitfile
# does not point to a valid repository", with no "Permission denied" to name it. A rule's fd there
# stays open across the exec (PINNED), holding the dentry, so every lookup beneath finds that inode.
UNSTABLE_INODES = {"9p"}
MOUNTINFO = "/proc/self/mountinfo"
PINNED: list[int] = []


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


def unstable_devices() -> set[int]:
    """The devices (st_dev) of the mounts whose inodes don't outlast their dentry (UNSTABLE_INODES);
    none when the mount table can't be read."""
    out = set()
    try:
        with open(MOUNTINFO) as f:
            for line in f:
                # id parent major:minor root point options [optional...] - fstype source super
                head, _, tail = line.partition(" - ")
                fields, kind = head.split(), tail.split()[:1]
                if len(fields) > 2 and kind and kind[0] in UNSTABLE_INODES:
                    major, _, minor = fields[2].partition(":")
                    if major.isdigit() and minor.isdigit():
                        out.add(os.makedev(int(major), int(minor)))
    except OSError:
        pass
    return out


def restrict(writable: list[str], readable: list[str] | None = None,
             unix: list[str] | None = None) -> int:
    """Forbid this process and its children every write outside `writable` (directories, and
    files such as /dev/null) and, given `readable`, reading a file outside it and `writable`;
    connecting to a UNIX socket made outside the lane except beneath `unix`, and signalling a
    process outside it. A rule on a mount of UNSTABLE_INODES keeps its fd open into the exec
    (PINNED). Returns the ABI used, 0 when Landlock isn't there."""
    version = abi()
    libc = _libc()
    # Before Landlock: no-new-privileges holds on any Linux, so sudo is refused even where the
    # lane can't be drawn (the 2026-11-16 audit: it was set only with Landlock, and the docs
    # promised it everywhere). Without Landlock a failure here is let be: that path execs
    # unrestricted; with it, restrict_self needs it, so it fails closed below.
    if sys.platform.startswith("linux"):
        try:
            nnp = libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)
        except AttributeError:
            nnp = 0
        if nnp < 0 and version:
            raise OSError(ctypes.get_errno(), "prctl")
    if not version:
        return 0
    write = WRITE_FILE | REMOVE_DIR | REMOVE_FILE | MAKE
    if version >= 2:
        write |= REFER   # always denied unless handled: handled, a move inside the lane works
    if version >= 3:
        write |= TRUNCATE
    handled = write | (READ_FILE if readable is not None else 0)
    # Handled and granted nowhere but `unix`: the agent can't reach the user's session bus (and
    # through systemd-run, a command outside the lane), keyring or SSH agent by their paths (the
    # 2026-11-16 audit). Sockets the lane's own processes make are theirs whatever the rules.
    if version >= 9:
        handled |= RESOLVE_UNIX
    if version >= 6:  # handled_access_net stays 0: the network (the model servers) stays free
        attr = ctypes.create_string_buffer(
            struct.pack("=QQQ", handled, 0, SCOPE_ABSTRACT_UNIX | SCOPE_SIGNAL), 24)
        size = 24
    else:
        attr, size = ctypes.c_uint64(handled), 8
    fd = libc.syscall(CREATE_RULESET, ctypes.byref(attr), ctypes.c_size_t(size), ctypes.c_uint32(0))
    if fd < 0:
        raise OSError(ctypes.get_errno(), "landlock_create_ruleset")
    unstable = unstable_devices()
    try:
        read = handled & READ_FILE
        sock = handled & RESOLVE_UNIX
        # `unix` gets the socket right alone: its reads and writes come from the split lists
        # (the 0.90 review: `read` there made a granted ~/.config's gh token readable again)
        for path, base in [*((p, write | read) for p in writable),
                           *((p, read) for p in readable or []),
                           *((p, sock) for p in unix or [])]:
            try:
                pfd = os.open(path, os.O_PATH | os.O_CLOEXEC)
            except OSError:
                continue  # gone since the lane was drawn
            pin = False
            try:
                rights = base if os.path.isdir(path) else \
                    base & (WRITE_FILE | READ_FILE | TRUNCATE | RESOLVE_UNIX)
                if not rights:
                    continue
                rule = ctypes.create_string_buffer(struct.pack("=Qi", rights, pfd))  # packed
                if libc.syscall(ADD_RULE, fd, RULE_PATH_BENEATH, rule,
                                ctypes.c_uint32(0)) < 0:
                    err = ctypes.get_errno()
                    # a file that is no file here (a pipe or socket a /dev/std* link reached):
                    # no rule grants it, so leaving it out takes nothing; refusing the whole lane
                    # failed every check run with its output piped (#1308, 2026-11-24)
                    if err == errno.EBADFD and not os.path.isdir(path):
                        continue  # pin is still False: the finally closes its fd
                    raise OSError(err, f"landlock_add_rule {path}")
                pin = os.fstat(pfd).st_dev in unstable
            finally:
                if pin:  # O_PATH: it reads and writes nothing, and the rules still hold through it
                    os.set_inheritable(pfd, True)
                    PINNED.append(pfd)
                else:
                    os.close(pfd)
        if libc.syscall(RESTRICT_SELF, fd, ctypes.c_uint32(0)) < 0:
            raise OSError(ctypes.get_errno(), "landlock_restrict_self")
    finally:
        os.close(fd)
    return version


def gaps(version: int) -> list[str]:
    """What this Landlock version can't confine, for `wb doctor` (the 2026-11-16 audit: ABI 1-2
    kernels, Debian 12, Ubuntu 22.04, WSL2's 5.15, were reported as a whole lane)."""
    out = []
    if 0 < version < 3:
        out.append("truncating a file outside the lane (truncate(2)) isn't refused before "
                   "ABI 3 (Linux 6.2)")
    if version == 1:
        out.append("moving or hard-linking a file between directories fails even inside the "
                   "lane (\"Invalid cross-device link\"; most tools fall back to a copy) before "
                   "ABI 2 (Linux 5.19)")
    if 0 < version < 6:
        out.append("the agent can signal the user's other processes before ABI 6 (Linux 6.12)")
    if 0 < version < 9:
        out.append("the agent can connect to the user's UNIX sockets (the session bus, so "
                   "systemd-run; the SSH and GPG agents) before ABI 9")
    return out


def wrap(writable: list[str] | None, cmd: list[str],
         readable: list[str] | None = None, unix: list[str] | None = None) -> list[str]:
    """The command that runs `cmd` inside the lane; `cmd` itself when there is no lane."""
    if writable is None:
        return cmd
    return [sys.executable, "-I", os.path.abspath(__file__),
            *(a for p in writable for a in ("--write", p)),
            *(["--reads"] if readable is not None else []),
            *(a for p in readable or [] for a in ("--read", p)),
            *(a for p in unix or [] for a in ("--unix", p)), "--", *cmd]


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


# how a refused lane's output starts, which wb verify reads as no evidence (#1308)
REFUSED = "workbench lane: the kernel refused the lane"


def main(argv: list[str]) -> None:
    die_with_parent()
    sep = argv.index("--")
    pick = lambda flag: [argv[i + 1] for i in range(sep) if argv[i] == flag]  # noqa: E731
    readable = pick("--read") if "--reads" in argv[:sep] else None
    try:
        restrict(pick("--write"), readable, pick("--unix"))
    except OSError as e:  # Landlock is there but refused the lane: don't run outside it
        print(f"{REFUSED} ({e}); not running "
              f"{argv[sep + 1]}", file=sys.stderr)
        sys.exit(126)
    try:
        os.execvp(argv[sep + 1], argv[sep + 1:])
    except OSError as e:  # a traceback in runner.log, and the round only "exited 1" (the 2026-11-16 audit)
        print(f"workbench lane: can't run {argv[sep + 1]} ({e.strerror})", file=sys.stderr)
        sys.exit(127 if isinstance(e, FileNotFoundError) else 126)


if __name__ == "__main__":
    main(sys.argv[1:])
