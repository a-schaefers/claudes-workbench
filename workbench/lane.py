"""The agent writes only inside its lane, enforced by the kernel where it can be: Linux Landlock, or
macOS Seatbelt (sandbox-exec).

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

On macOS (#1194) the same entry point execs `/usr/bin/sandbox-exec` with a profile drawn fresh
from the same lists (`profile`): writes only beneath `--write`, a UNIX socket only beneath
`--unix`, signals and process information only within the sandbox, and no sudo, su or login.
Reads there are the read lane's complement, `--hide` and `--show` in context.read_rules' order
(ancestors first), the last match winning as Seatbelt's own rules do: Seatbelt can't allow reads
by a list the way Landlock does without naming the whole of dyld and /System.

No-new-privileges (no sudo, su or other setuid program) is set on every Linux machine, Landlock
or not. Without either (an old or locked-down kernel, a Mac without sandbox-exec) it otherwise
execs the command unrestricted and `wb doctor` says so. A kernel that has Landlock but refuses
the lane, or a sandbox-exec that refuses the profile, stops the command instead: the agent never
runs believed to be in a lane it isn't in. Only the standard library here: it runs before every
round.
"""
from __future__ import annotations

import ctypes
import os
import platform
import shutil
import signal
import struct
import subprocess
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
# macOS (#1194): ships with every release (deprecated by Apple, still what Codex relies on)
SANDBOX_EXEC = "/usr/bin/sandbox-exec"
# What every process opens there, read and written inside the sandbox as on any Mac: the fd
# directory (bash's process substitution, /dev/stdout), dyld's DTrace helper, the terminal
SEATBELT_DEVICES = ("/dev/fd", "/dev/stdin", "/dev/stdout", "/dev/stderr", "/dev/dtracehelper",
                    "/dev/null", "/dev/zero", "/dev/random", "/dev/urandom", "/dev/tty")
# and the system's sockets every process may use: name lookups (getaddrinfo asks mDNSResponder
# there, so without it no model server resolves) and the log
SEATBELT_SOCKETS = ("/private/var/run/mDNSResponder", "/private/var/run/syslog")
# Seatbelt's stand-in for no-new-privileges, by path: a setuid-bit rule would refuse /bin/ps,
# setuid root on macOS, and the tools that run it; and what starts a program outside the
# sandbox by name (LaunchServices' `open`, launchd's `launchctl`, AppleScript's `osascript`)
SEATBELT_REFUSED = ("/usr/bin/sudo", "/usr/bin/su", "/usr/bin/login",
                    "/usr/libexec/security_authtrampoline", "/usr/bin/open", "/bin/launchctl",
                    "/usr/bin/osascript")


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


def kind() -> str | None:
    """The lane this machine draws: "landlock" (Linux), "seatbelt" (macOS with sandbox-exec),
    else None."""
    if abi():
        return "landlock"
    if sys.platform == "darwin" and os.access(SANDBOX_EXEC, os.X_OK):
        return "seatbelt"
    return None


def held() -> bool:
    """Whether a lane holds the agent here: the question "is there a lane" (doctor, setup,
    lane_refused, the API note), on either kernel."""
    return kind() is not None


def restrict(writable: list[str], readable: list[str] | None = None,
             unix: list[str] | None = None) -> int:
    """Forbid this process and its children every write outside `writable` (directories, and
    files such as /dev/null) and, given `readable`, reading a file outside it and `writable`;
    connecting to a UNIX socket made outside the lane except beneath `unix`, and signalling a
    process outside it. Returns the ABI used, 0 when Landlock isn't there."""
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
        if libc.syscall(RESTRICT_SELF, fd, ctypes.c_uint32(0)) < 0:
            raise OSError(ctypes.get_errno(), "landlock_restrict_self")
    finally:
        os.close(fd)
    return version


def gaps(version: int | str) -> list[str]:
    """What this Landlock version, or "seatbelt", can't confine, for `wb doctor` (the 2026-11-16
    audit: ABI 1-2 kernels, Debian 12, Ubuntu 22.04, WSL2's 5.15, were reported as a whole lane)."""
    if version == "seatbelt":
        return list(SEATBELT_GAPS)
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


SEATBELT_GAPS = (
    "sandbox-exec is deprecated by Apple: it works on current macOS, and a release may drop it",
    "a sandbox can't be made inside it: SwiftPM's manifest builds need --disable-sandbox, and "
    "Chromium and Playwright --no-sandbox",
    "reads are refused by a list (credentials, workbench's state, your home's own folders, other "
    "users'), not allowed by one as Landlock does: a credential kept anywhere else is readable",
    "a UNIX socket in a temp directory can't be connected to, even one the agent's tests made "
    "(Seatbelt can't tell whose it is: tmux's and the SSH agent's are there): `wb allow --path` "
    "the directory",
    "macOS's own services stay reachable over Mach (a program can still ask LaunchServices to "
    "start an app outside the lane; open, launchctl and osascript are refused by name)",
    "TCP isn't refused, as on Linux")


def wrap(writable: list[str] | None, cmd: list[str],
         readable: list[str] | None = None, unix: list[str] | None = None,
         reads: list[tuple[str, str]] | None = None,
         deny_write: list[str] | None = None) -> list[str]:
    """The command that runs `cmd` inside the lane; `cmd` itself when there is no lane.
    `readable` is Landlock's read lane, `reads` Seatbelt's: ("deny" | "allow", path) in order;
    `deny_write` Seatbelt's places refused inside `writable` (Landlock's lane is split instead)."""
    if writable is None:
        return cmd
    return [sys.executable, "-I", os.path.abspath(__file__),
            *(a for p in writable for a in ("--write", p)),
            *(a for p in deny_write or [] for a in ("--deny-write", p)),
            *(["--reads"] if readable is not None else []),
            *(a for p in readable or [] for a in ("--read", p)),
            *(a for e, p in reads or [] for a in ("--hide" if e == "deny" else "--show", p)),
            *(a for p in unix or [] for a in ("--unix", p)), "--", *cmd]


def profile(writable: list[str], reads: list[tuple[str, str]] | None = None,
            unix: list[str] | None = None,
            deny_write: list[str] | None = None) -> tuple[str, list[str]]:
    """The Seatbelt profile for a lane, and its parameters as sandbox-exec's `-D` arguments:
    every path is a parameter (`(param "W0")`), so none is quoted into the text. Real paths:
    Seatbelt matches the resolved path, and /tmp is /private/tmp, /var /private/var.
    Everything is allowed but what is named: Seatbelt applies the last rule that matches."""
    params: list[str] = []

    def ref(prefix: str, path: str, real: bool = True) -> str:
        name = f"{prefix}{len(params)}"
        params.append(f"-D{name}={os.path.realpath(path) if real else path}")
        return f'(subpath (param "{name}"))'

    def lines(effect: str, ops: str, refs: list[str]) -> list[str]:
        return [f"({effect} {ops} {' '.join(refs)})"] if refs else []

    write = [ref("W", p) for p in dict.fromkeys(writable)]
    # and what is refused inside the lane, after it (the last match wins), made or not: Apple's
    # Python bytecode in the whole Library/Caches (#1194's second review)
    denied = [ref("N", p) for p in dict.fromkeys(deny_write or [])]
    text = ["(version 1)", "(allow default)",
            # writes: the lane alone; file-write* takes in chmod, utimes and xattrs, which
            # Landlock leaves alone
            "(deny file-write*)", *lines("allow", "file-write*", write),
            *lines("deny", "file-write*", denied),
            # a hard link out of the lane would carry a file in, to be read or written there
            # (Landlock's REFER refuses the same)
            "(deny file-link)", *lines("allow", "file-link", write), *lines("deny", "file-link", denied)]
    # reads: as given (read_rules: ancestors first), each over the last; the contents only
    # (file-read-data: a file's or a listing's), never the metadata, so a stat or realpath
    # through a hidden directory (the worktree, under workbench's state) works as on Linux
    text += [f"({effect} file-read-data {ref('R', p)})" for effect, p in reads or []]
    text += lines("allow", "file-read-data", write)  # the lane is read too, as with Landlock
    text += lines("allow", "file-read* file-write*", [ref("D", d, False) for d in SEATBELT_DEVICES])
    # a UNIX socket only beneath `unix` (Landlock ABI 9's rule): the user's agents and tmux
    # outside the lane stay out of reach
    text += ["(deny network-outbound (remote unix-socket))",
             *lines("allow", "network-outbound", [*(ref("U", p) for p in dict.fromkeys(unix or [])),
                                                  *(ref("S", p, False) for p in SEATBELT_SOCKETS)])]
    # no signal to, or reading of, a process outside the sandbox (ABI 6's signal scope)
    text += ["(deny signal)", "(allow signal (target same-sandbox))",
             "(deny process-info*)", "(allow process-info* (target same-sandbox))"]
    text += [f"(deny process-exec {' '.join(ref('X', p, False) for p in SEATBELT_REFUSED)})"]
    return "\n".join(text) + "\n", params


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


def seatbelt(writable: list[str], reads: list[tuple[str, str]], unix: list[str],
             cmd: list[str], deny_write: list[str] | None = None) -> None:
    """Exec `cmd` under sandbox-exec with the lane's profile; never outside it. The profile is
    tried first on /usr/bin/true: a refused one (a macOS that dropped a rule) stops the command
    with lane.py's own message and 126, not sandbox-exec's exit that reads as the agent's."""
    text, params = profile(writable, reads, unix, deny_write)
    base = [SANDBOX_EXEC, "-p", text, *params]
    try:
        tried = subprocess.run([*base, "/usr/bin/true"], stdin=subprocess.DEVNULL,
                               capture_output=True, text=True, errors="replace", timeout=60)
        why = (tried.stderr.strip()[-300:] or f"exit {tried.returncode}") if tried.returncode else ""
    except (OSError, subprocess.SubprocessError) as e:
        why = str(e)
    if why:
        print(f"workbench lane: macOS refused the lane ({why}); not running {cmd[0]}",
              file=sys.stderr)
        sys.exit(126)
    if not shutil.which(cmd[0]):  # as Landlock's side says it: sandbox-exec's own exit isn't 127
        print(f"workbench lane: can't run {cmd[0]} (No such file or directory)", file=sys.stderr)
        sys.exit(127)
    os.execv(SANDBOX_EXEC, [*base, "--", *cmd])


def main(argv: list[str]) -> None:
    die_with_parent()
    sep = argv.index("--")
    pick = lambda flag: [argv[i + 1] for i in range(sep) if argv[i] == flag]  # noqa: E731
    readable = pick("--read") if "--reads" in argv[:sep] else None
    if kind() == "seatbelt":
        reads = [("deny" if argv[i] == "--hide" else "allow", argv[i + 1])
                 for i in range(sep) if argv[i] in ("--hide", "--show")]
        try:
            seatbelt(pick("--write"), reads, pick("--unix"), argv[sep + 1:], pick("--deny-write"))
        except OSError as e:
            print(f"workbench lane: can't run {argv[sep + 1]} ({e.strerror})", file=sys.stderr)
            sys.exit(126)
        return  # never Landlock's below: execv doesn't return, but a test's stand-in does
    try:
        restrict(pick("--write"), readable, pick("--unix"))
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
