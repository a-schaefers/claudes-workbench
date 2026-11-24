"""The agent writes only inside its lane, enforced by the kernel where it can be (Linux Landlock).

The runner starts OpenCode through this file (`python -I lane.py --write DIR... -- opencode ...`),
which restricts itself and then execs the command, so OpenCode, its shell and everything that runs
in it (make, an interpreter, a test suite, the MCP server) inherit the limit. Writes are handled,
and reading files when `--read` names what may be read (context.py: everything but the places
credentials live; running a program reads it, so it must be readable too). Where the kernel can
(ABI 9, ABI 6), it also refuses connecting to a UNIX socket made outside the lane (the session
bus, the SSH and GPG agents: `--unix` names the places that may), an abstract socket, or a signal
to a process outside it (the runner, other tasks, Claude Code). From ABI 4 it refuses a TCP connect
to each `--deny-port` (context.deny_ports: local daemons that take commands, a debugger's port),
on any address: Landlock only allows ports, so every other port gets a rule; its rule sees TCP
sockets only, so a seccomp filter refuses making an MPTCP or SMC one then, and io_uring (which
makes sockets past the filter), as a kernel without them does. Listing directories,
binding and the rest of the network stay as they were. Landlock doesn't cover metadata: chmod,
chown, utimes and xattrs on a file outside the lane still succeed. A write or a denied connect
fails with "Permission denied", and the agent asks Claude for the directory (`wb allow --path`)
or the port (`wb allow --port`). context.py decides the lane; OpenCode's own
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
RULE_PATH_BENEATH, RULE_NET_PORT = 1, 2
PR_SET_NO_NEW_PRIVS = 38
PR_SET_PDEATHSIG = 1
WRITE_FILE, READ_FILE, REMOVE_DIR, REMOVE_FILE = 1 << 1, 1 << 2, 1 << 4, 1 << 5
MAKE = sum(1 << b for b in range(6, 13))        # char, dir, reg, sock, fifo, block, sym
REFER, TRUNCATE = 1 << 13, 1 << 14               # ABI 2 and 3
RESOLVE_UNIX = 1 << 16                           # ABI 9: connect to a socket made outside
SCOPE_ABSTRACT_UNIX, SCOPE_SIGNAL = 1, 2         # ABI 6: ruleset_attr.scoped
CONNECT_TCP = 1 << 1                             # ABI 4: ruleset_attr.handled_access_net

# seccomp, for the stream sockets Landlock's TCP rule doesn't see (#1197's review, 2026-11-24):
# an MPTCP socket (protocol 262) connected to a denied port on Linux 7.2, ABI 10, and SMC's
# hands its connect to a kernel TCP socket; io_uring makes sockets with no socket(2) to filter
PR_SET_SECCOMP, SECCOMP_MODE_FILTER = 22, 2
RET_ALLOW, RET_ERRNO = 0x7FFF0000, 0x00050000
LD_ABS, JEQ, RET = 0x20, 0x15, 0x06              # BPF_LD|W|ABS, BPF_JMP|JEQ|K, BPF_RET|K
AF_INET, AF_INET6, AF_SMC = 2, 10, 43
IPPROTO_SMC, IPPROTO_MPTCP = 256, 262
X86_64, X32 = 0xC000003E, 0x40000000           # x32's bit on x86_64's syscall numbers
IO_URING_SETUP = 425                              # the same on every arch the unified table has
# AUDIT_ARCH_* and socket(2)'s number on it, the compat ones too (a 32-bit program on a 64-bit
# kernel). socketcall(2) (i386, ppc, s390) passes its arguments in memory, which seccomp can't
# read: a 32-bit x86 program calling it still makes an MPTCP socket
SOCKET_NR = {X86_64: (41, X32 | 41), 0x40000003: (359,), 0xC00000B7: (198,),
             0x40000028: (281,), 0xC00000F3: (198,), 0xC0000102: (198,), 0xC0000015: (326,),
             0x80000015: (326,), 0x00000014: (326,), 0x80000016: (359,), 0x00000016: (359,)}


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


def restrict(writable: list[str], readable: list[str] | None = None,
             unix: list[str] | None = None, deny_ports: list[int] | None = None) -> int:
    """Forbid this process and its children every write outside `writable` (directories, and
    files such as /dev/null) and, given `readable`, reading a file outside it and `writable`;
    connecting to a UNIX socket made outside the lane except beneath `unix`, signalling a
    process outside it, and connecting over TCP to a port of `deny_ports`. Returns the ABI
    used, 0 when Landlock isn't there."""
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
    # TCP connect handled only with ports to refuse, each other port allowed by a rule of its
    # own: Landlock lists the ports allowed, never those refused, so the agent's tests still
    # reach the servers they start on any port (#1197, 2026-11-24: 65,530 rules and
    # restrict_self took 0.05 s on ABI 10). Binding stays unhandled.
    deny = {p for p in deny_ports or () if 0 <= p <= 65535} if version >= 4 else set()
    net = CONNECT_TCP if deny else 0
    if version >= 6:
        attr = ctypes.create_string_buffer(
            struct.pack("=QQQ", handled, net, SCOPE_ABSTRACT_UNIX | SCOPE_SIGNAL), 24)
        size = 24
    elif version >= 4:  # handled_access_net, and no `scoped` before ABI 6
        attr, size = ctypes.create_string_buffer(struct.pack("=QQ", handled, net), 16), 16
    else:
        attr, size = ctypes.c_uint64(handled), 8
    fd = libc.syscall(CREATE_RULESET, ctypes.byref(attr), ctypes.c_size_t(size), ctypes.c_uint32(0))
    if fd < 0:
        raise OSError(ctypes.get_errno(), "landlock_create_ruleset")
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
            try:
                rights = base if os.path.isdir(path) else \
                    base & (WRITE_FILE | READ_FILE | TRUNCATE | RESOLVE_UNIX)
                if not rights:
                    continue
                rule = ctypes.create_string_buffer(struct.pack("=Qi", rights, pfd))  # packed
                if libc.syscall(ADD_RULE, fd, RULE_PATH_BENEATH, rule,
                                ctypes.c_uint32(0)) < 0:
                    raise OSError(ctypes.get_errno(), f"landlock_add_rule {path}")
            finally:
                os.close(pfd)
        if net:
            _allow_ports(libc, fd, deny)
        if libc.syscall(RESTRICT_SELF, fd, ctypes.c_uint32(0)) < 0:
            raise OSError(ctypes.get_errno(), "landlock_restrict_self")
    finally:
        os.close(fd)
    if net:  # else a denied port is a socket(…, 262) away
        refuse_unseen_streams(libc)
    return version


def stream_filter() -> bytes:
    """The seccomp program refusing what Landlock's TCP rule doesn't see: an MPTCP or SMC socket
    on IPv4 or IPv6 gets EPROTONOSUPPORT (as on a kernel without them: curl --mptcp and the
    rest fall back to TCP, which the rule covers), an AF_SMC one EAFNOSUPPORT, io_uring_setup
    ENOSYS (as where it's off). Everything else is allowed."""
    big = sys.byteorder == "big"
    arg = lambda i: 16 + 8 * i + (4 if big else 0)  # noqa: E731 an int argument's low word
    prog: list[tuple] = [("ld", 4)]                 # seccomp_data.arch
    arches = sorted(SOCKET_NR)
    prog += [("jeq", a, f"arch{a}") for a in arches] + [("ret", RET_ALLOW)]
    for a in arches:
        prog += [("label", f"arch{a}"), ("ld", 0)]   # seccomp_data.nr
        prog += [("jeq", nr, "socket") for nr in SOCKET_NR[a]]
        prog += [("jeq", nr, "nosys") for nr in (IO_URING_SETUP, X32 | IO_URING_SETUP)[
            :2 if a == X86_64 else 1]]
        prog.append(("ret", RET_ALLOW))
    prog += [("label", "socket"), ("ld", arg(0)), ("jeq", AF_SMC, "noaf"),
             ("jeq", AF_INET, "proto"), ("jeq", AF_INET6, "proto"), ("ret", RET_ALLOW),
             ("label", "proto"), ("ld", arg(2)), ("jeq", IPPROTO_MPTCP, "noproto"),
             ("jeq", IPPROTO_SMC, "noproto"), ("ret", RET_ALLOW),
             ("label", "noproto"), ("ret", RET_ERRNO | errno.EPROTONOSUPPORT),
             ("label", "noaf"), ("ret", RET_ERRNO | errno.EAFNOSUPPORT),
             ("label", "nosys"), ("ret", RET_ERRNO | errno.ENOSYS)]
    at, n = {}, 0
    for op in prog:
        if op[0] == "label":
            at[op[1]] = n
        else:
            n += 1
    out, n = [], 0
    for op in prog:
        if op[0] == "label":
            continue
        n += 1
        if op[0] == "ld":
            out.append(struct.pack("=HBBI", LD_ABS, 0, 0, op[1]))
        elif op[0] == "ret":
            out.append(struct.pack("=HBBI", RET, 0, 0, op[1]))
        else:  # true: the label, relative to the next instruction; false: the next
            jt = at[op[2]] - n
            if not 0 <= jt < 256:
                raise ValueError(f"seccomp jump {jt}")
            out.append(struct.pack("=HBBI", JEQ, jt, 0, op[1]))
    return b"".join(out)


def refuse_unseen_streams(libc) -> None:
    """Install stream_filter on this process and what it execs (no-new-privileges is set)."""
    code = stream_filter()
    buf = ctypes.create_string_buffer(code, len(code))

    class Prog(ctypes.Structure):  # struct sock_fprog
        _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.c_void_p)]
    prog = Prog(len(code) // 8, ctypes.addressof(buf))
    mode = ctypes.c_ulong(SECCOMP_MODE_FILTER)
    if libc.prctl(PR_SET_SECCOMP, mode, ctypes.byref(prog), 0, 0) < 0:
        raise OSError(ctypes.get_errno(), "seccomp")


def _allow_ports(libc, fd: int, deny: set[int]) -> None:
    """A connect rule for every TCP port but those of `deny`, one buffer reused (speed)."""
    rule = ctypes.create_string_buffer(16)  # landlock_net_port_attr: allowed_access, port
    add, flags = libc.syscall, ctypes.c_uint32(0)
    for port in range(65536):
        if port in deny:
            continue
        struct.pack_into("=QQ", rule, 0, CONNECT_TCP, port)
        if add(ADD_RULE, fd, RULE_NET_PORT, rule, flags) < 0 and port:  # 0: no connect's
            raise OSError(ctypes.get_errno(), f"landlock_add_rule port {port}")


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
    if 0 < version < 4:
        out.append("the agent can connect to local daemons' TCP ports (a Docker API, adb, a "
                   "debugger's port) before ABI 4 (Linux 6.7)")
    if 0 < version < 6:
        out.append("the agent can signal the user's other processes before ABI 6 (Linux 6.12)")
    if 0 < version < 9:
        out.append("the agent can connect to the user's UNIX sockets (the session bus, so "
                   "systemd-run; the SSH and GPG agents) before ABI 9")
    return out


def wrap(writable: list[str] | None, cmd: list[str], readable: list[str] | None = None,
         unix: list[str] | None = None, deny_ports: list[int] | None = None) -> list[str]:
    """The command that runs `cmd` inside the lane; `cmd` itself when there is no lane."""
    if writable is None:
        return cmd
    return [sys.executable, "-I", os.path.abspath(__file__),
            *(a for p in writable for a in ("--write", p)),
            *(["--reads"] if readable is not None else []),
            *(a for p in readable or [] for a in ("--read", p)),
            *(a for p in unix or [] for a in ("--unix", p)),
            *(a for p in sorted(deny_ports or ()) for a in ("--deny-port", str(p))), "--", *cmd]


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
    pick = lambda flag: [argv[i + 1] for i in range(sep) if argv[i] == flag]  # noqa: E731
    readable = pick("--read") if "--reads" in argv[:sep] else None
    deny = [int(p) for p in pick("--deny-port")]  # wrap's own numbers: a bad one is a traceback
    try:
        restrict(pick("--write"), readable, pick("--unix"), deny)
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
