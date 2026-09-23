#!/usr/bin/env python3
"""Kernel-enforced syscall confinement for workloads that run without a container.

This harness has always had two execution paths: a rootless container when `docker` or
`podman` is present, and a "simulator" subprocess when neither is. The simulator is the
path that actually runs on most developer machines and inside most CI jobs — and it
enforced none of the eight controls the auditor scored, because the auditor read the
*policy object* rather than the execution. A default policy therefore reported 100.0/100
with zero violations while the same harness ran code that opened a socket and wrote a file
into the invoking user's home directory. That is [Observation 11]'s silent certification,
in the component whose entire job is containment.

This module closes it with the one mechanism available to an unprivileged process on
Linux: a seccomp-BPF filter installed with `prctl(2)`, filtering system calls in the
kernel. No dependency, no daemon, no root — `ctypes` and the C library, in keeping with
the zero-dependency rule for sample applications.

Four properties are load-bearing, and each is a documented way to build a filter that
looks like confinement and is not.

**The architecture is checked first.** System call numbers are per-architecture, so a
filter that matches numbers without pinning `AUDIT_ARCH` denies whatever those numbers
happen to mean under another ABI. Anything that is not the architecture the table was
written for is killed outright rather than allowed.

**The x32 ABI is killed outright.** On x86-64, an x32 caller passes the `AUDIT_ARCH_X86_64`
check and carries `__X32_SYSCALL_BIT` (`0x40000000`) in its call number, so every
comparison misses and every denied call is permitted. This is the classic seccomp bypass,
and the mitigation is one instruction.

**A name the table does not know is an error, never a skip.** A denylist that silently
drops the entries it could not resolve is narrower than it says it is, and nothing in its
output reveals which entries survived.

**The filter is installed in the child, after `fork` and before `exec`.** It cannot be
removed, and it is inherited across `execve` — which is why permitting `execve` costs
nothing: the binary the workload execs runs under the same filter. Installing it in the
parent would confine the harness, and the test suite running it.

[Observation 11]: ../../observations/systems/11-silent-certification-failure-and-gate-integrity.md
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import platform
import resource
from collections.abc import Callable, Iterable
from typing import Final

PR_SET_NO_NEW_PRIVS: Final[int] = 38
PR_SET_SECCOMP: Final[int] = 22
SECCOMP_MODE_FILTER: Final[int] = 2

SECCOMP_RET_KILL_PROCESS: Final[int] = 0x80000000
SECCOMP_RET_ERRNO: Final[int] = 0x00050000
SECCOMP_RET_ALLOW: Final[int] = 0x7FFF0000
EPERM: Final[int] = 1

# Refusing with an errno rather than killing, for the one group where killing is wrong.
DENY_EPERM: Final[int] = SECCOMP_RET_ERRNO | EPERM

# The x32 ABI shares AUDIT_ARCH_X86_64 and sets this bit in every call number.
X32_SYSCALL_BIT: Final[int] = 0x40000000

AUDIT_ARCH_X86_64: Final[int] = 0xC000003E

BPF_LD: Final[int] = 0x00
BPF_W: Final[int] = 0x00
BPF_ABS: Final[int] = 0x20
BPF_JMP: Final[int] = 0x05
BPF_JEQ: Final[int] = 0x10
BPF_JGE: Final[int] = 0x30
BPF_K: Final[int] = 0x00
BPF_RET: Final[int] = 0x06

# Offsets into `struct seccomp_data`: the call number, then the architecture.
_OFFSET_NR: Final[int] = 0
_OFFSET_ARCH: Final[int] = 4

# Only x86-64 is tabulated, and every other architecture is refused rather than guessed.
# A syscall number carried over from another ABI denies a different call than the one
# named, which is a filter that reports confinement it is not applying.
SUPPORTED_ARCH: Final[str] = "x86_64"

# Verified against the kernel's own `asm/unistd_64.h` by `test_syscall_filter.py`, which is
# the only thing standing between this table and a transcription error nothing would catch.
SYSCALLS: Final[dict[str, int]] = {
    "accept": 43, "accept4": 288, "bind": 49, "connect": 42, "listen": 50,
    "recvfrom": 45, "recvmsg": 47, "sendmsg": 46, "sendto": 44,
    "socket": 41, "socketpair": 53,
    "capset": 126, "ioperm": 173, "iopl": 172,
    "setgid": 106, "setregid": 114, "setresgid": 119, "setresuid": 117,
    "setreuid": 113, "setuid": 105,
    "perf_event_open": 298, "process_vm_readv": 310, "process_vm_writev": 311, "ptrace": 101,
    "chroot": 161, "mount": 165, "pivot_root": 155, "setns": 308, "umount2": 166, "unshare": 272,
    "adjtimex": 159, "bpf": 321, "clock_settime": 227, "delete_module": 176,
    "finit_module": 313, "init_module": 175, "kexec_file_load": 320, "kexec_load": 246,
    "reboot": 169, "settimeofday": 164, "swapoff": 168, "swapon": 167, "userfaultfd": 323,
    "add_key": 248, "keyctl": 250, "request_key": 249,
    "clone": 56, "clone3": 435, "fork": 57, "vfork": 58,
    "io_uring_setup": 425, "io_uring_enter": 426, "io_uring_register": 427,
}

DENY_GROUPS: Final[dict[str, tuple[str, ...]]] = {
    # io_uring belongs here, not in a group of its own. It is a second, complete path to
    # every operation the calls above perform: a ring submitting IORING_OP_SOCKET,
    # IORING_OP_CONNECT and IORING_OP_SEND opens a connection without issuing `socket(2)`
    # at all, so a filter that denies the direct calls and leaves the ring reachable denies
    # nothing while reporting network isolation as ENFORCED. Container runtimes disable
    # io_uring for exactly this reason. Verified here: under the default policy `socket()`
    # raised EPERM while `io_uring_setup(2)` still reached the kernel.
    "network": ("socket", "socketpair", "bind", "connect", "listen", "accept", "accept4",
                "sendto", "recvfrom", "sendmsg", "recvmsg",
                "io_uring_setup", "io_uring_enter", "io_uring_register"),
    "privilege": ("setuid", "setgid", "setreuid", "setregid", "setresuid", "setresgid",
                  "capset", "ioperm", "iopl"),
    "tracing": ("ptrace", "process_vm_readv", "process_vm_writev", "perf_event_open"),
    "mount": ("mount", "umount2", "pivot_root", "chroot", "unshare", "setns"),
    "kernel": ("init_module", "delete_module", "finit_module", "kexec_load", "kexec_file_load",
               "reboot", "settimeofday", "clock_settime", "adjtimex", "swapon", "swapoff",
               "bpf", "userfaultfd"),
    "keyring": ("add_key", "keyctl", "request_key"),
    # Absolute rather than numeric: the workload may not create a process at all. Off by
    # default because a payload that legitimately spawns one would die with SIGSYS, and a
    # containment control that breaks ordinary work gets disabled rather than tuned.
    "process_creation": ("clone", "clone3", "fork", "vfork"),
}

# What a denied call does. Killing is the right answer for a call no ordinary workload
# makes, and the wrong answer for `socket`.
#
# The C library opens a socket to resolve users and groups through NSS, so a filter that
# kills on `socket` kills any Python started without `HOME` in its environment: `site.py`
# computes the user site directory, `expanduser` falls back to `pwd.getpwuid`, and the
# interpreter dies with SIGSYS before it runs a line of the workload. Observed here, on a
# harness that strips the environment down to five variables and therefore always hits it.
#
# Refusing with `EPERM` fixes it without opening a hole: NSS falls back to `/etc/passwd`,
# and the workload's own socket call raises `PermissionError`, which is a denial it can
# report rather than a death it cannot. Filtering `socket`'s address family instead would
# have to permit `AF_UNIX` — and the Docker socket is an `AF_UNIX` socket.
GROUP_ACTIONS: Final[dict[str, int]] = {
    "network": DENY_EPERM,
    "privilege": SECCOMP_RET_KILL_PROCESS,
    "tracing": SECCOMP_RET_KILL_PROCESS,
    "mount": SECCOMP_RET_KILL_PROCESS,
    "kernel": SECCOMP_RET_KILL_PROCESS,
    "keyring": SECCOMP_RET_KILL_PROCESS,
    "process_creation": SECCOMP_RET_KILL_PROCESS,
}

DEFAULT_GROUPS: Final[tuple[str, ...]] = (
    "network", "privilege", "tracing", "mount", "kernel", "keyring",
)


class SeccompUnavailable(RuntimeError):
    """Raised when this kernel, architecture or policy cannot support a filter.

    Distinct from a filter that fails to install: the caller's response differs, because
    an unavailable filter must be *reported* as unenforced rather than silently skipped.
    """


class _SockFilter(ctypes.Structure):
    """One BPF instruction, in the layout `struct sock_filter` declares."""

    _fields_ = (
        ("code", ctypes.c_uint16),
        ("jt", ctypes.c_uint8),
        ("jf", ctypes.c_uint8),
        ("k", ctypes.c_uint32),
    )


class _SockFprog(ctypes.Structure):
    """The program header `prctl(PR_SET_SECCOMP)` expects."""

    _fields_ = (
        ("len", ctypes.c_ushort),
        ("filter", ctypes.POINTER(_SockFilter)),
    )


def resolve(groups: Iterable[str]) -> dict[str, int]:
    """Expand group names to `{syscall name: action}`, refusing anything unrecognised.

    Killing outranks refusing where a call appears in two groups, so widening the policy
    can never weaken what an already-denied call does.
    """
    wanted = list(groups)
    unknown = sorted(set(wanted) - set(DENY_GROUPS))
    if unknown:
        raise SeccompUnavailable(f"unknown deny group(s) {unknown}; known: {sorted(DENY_GROUPS)}")
    actions: dict[str, int] = {}
    for group in wanted:
        for name in DENY_GROUPS[group]:
            actions[name] = max(actions.get(name, 0), GROUP_ACTIONS[group])
    missing = sorted(name for name in actions if name not in SYSCALLS)
    if missing:
        raise SeccompUnavailable(f"no syscall number for {missing} on {SUPPORTED_ARCH}")
    return dict(sorted(actions.items()))


def build_program(actions: dict[str, int]) -> list[_SockFilter]:
    """Assemble the BPF program that refuses each named call with its own action.

    Two instructions per denied call and no jump arithmetic: each comparison either falls
    through to its own `ret`, carrying that call's action, or skips it. An off-by-one in a
    computed jump offset is a filter that denies the wrong call, and it is invisible in
    the emitted program.
    """
    denied = [(SYSCALLS[name], action) for name, action in actions.items()]
    program = [
        _SockFilter(BPF_LD | BPF_W | BPF_ABS, 0, 0, _OFFSET_ARCH),
        _SockFilter(BPF_JMP | BPF_JEQ | BPF_K, 1, 0, AUDIT_ARCH_X86_64),
        _SockFilter(BPF_RET | BPF_K, 0, 0, SECCOMP_RET_KILL_PROCESS),
        _SockFilter(BPF_LD | BPF_W | BPF_ABS, 0, 0, _OFFSET_NR),
        _SockFilter(BPF_JMP | BPF_JGE | BPF_K, 0, 1, X32_SYSCALL_BIT),
        _SockFilter(BPF_RET | BPF_K, 0, 0, SECCOMP_RET_KILL_PROCESS),
    ]
    for number, action in denied:
        program.append(_SockFilter(BPF_JMP | BPF_JEQ | BPF_K, 0, 1, number))
        program.append(_SockFilter(BPF_RET | BPF_K, 0, 0, action))
    program.append(_SockFilter(BPF_RET | BPF_K, 0, 0, SECCOMP_RET_ALLOW))
    return program


def supported() -> tuple[bool, str]:
    """Report whether a filter can be installed here, and why not when it cannot.

    Returned rather than raised because the answer belongs in the enforcement report. A
    sandbox that cannot confine must say so; one that says nothing is the defect this
    module exists to fix.
    """
    if platform.system() != "Linux":
        return False, f"seccomp is a Linux facility; this is {platform.system()}"
    if platform.machine() != SUPPORTED_ARCH:
        return False, f"no syscall table for {platform.machine()}; only {SUPPORTED_ARCH} is tabulated"
    return _probe()


def _probe() -> tuple[bool, str]:
    """Install a filter in a throwaway child, so the answer is the kernel's, not a guess."""
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:  # pragma: no cover - the child never returns to the test runner
        os.close(read_fd)
        try:
            install(("network",))
            os.write(write_fd, b"1")
        except (SeccompUnavailable, OSError):
            os.write(write_fd, b"0")
        os._exit(0)
    os.close(write_fd)
    with os.fdopen(read_fd, "rb") as stream:
        answer = stream.read()
    os.waitpid(pid, 0)
    return (True, "") if answer == b"1" else (False, "the kernel refused a seccomp filter")


def install(groups: Iterable[str] = DEFAULT_GROUPS) -> None:
    """Install the filter in the *current* process, irrevocably.

    `PR_SET_NO_NEW_PRIVS` must be set first: without it an unprivileged caller is refused
    with `EACCES`, because a filter that could hide a syscall from a setuid binary would
    be an escalation primitive rather than a confinement one.
    """
    installer(groups)()


def installer(groups: Iterable[str] = DEFAULT_GROUPS) -> Callable[[], None]:
    """Build everything now and return the one call to make after `fork`.

    The returned function runs between `fork` and `exec`, where allocating memory or taking
    a lock another thread holds can deadlock the child. Every structure, every symbol
    lookup and every validation happens here, in the parent, so the child does nothing but
    two `prctl` calls that are already resolved.
    """
    ok, reason = (True, "") if platform.system() == "Linux" else (False, "not Linux")
    if not ok:
        raise SeccompUnavailable(reason)
    program = build_program(resolve(groups))
    array = (_SockFilter * len(program))(*program)
    fprog = _SockFprog(len(program), ctypes.cast(array, ctypes.POINTER(_SockFilter)))
    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)

    def apply() -> None:
        """Set no-new-privs and the filter, in that order."""
        if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "PR_SET_NO_NEW_PRIVS failed")
        if libc.prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, ctypes.byref(fprog), 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "PR_SET_SECCOMP failed")

    # Held so the filter memory outlives this frame; the kernel copies it, but the cast
    # keeps no reference to `array` that Python can see.
    apply.__dict__["_program"] = (array, fprog)
    return apply


def limiter(memory_mb: int, cpu_seconds: int, max_processes: int) -> Callable[[], None]:
    """Return the resource caps to apply in the child, beside the filter.

    `RLIMIT_NPROC` is per *user*, not per process tree, so it is compared against every
    process the invoking uid already owns. That makes it fail closed — a busy machine may
    refuse the workload a fork it would otherwise have been allowed — which is the correct
    direction for a sandbox and the wrong direction for a benchmark, so it is reported as
    what it is rather than as a container PID cap.

    `RLIMIT_CORE` is zeroed because every denied call kills the process with `SIGSYS`, and
    a confinement control that fills the disk with core dumps gets turned off.
    """
    def apply() -> None:
        """Apply every cap, hard and soft together so the workload cannot raise them."""
        resource.setrlimit(resource.RLIMIT_AS, (memory_mb * 1024 * 1024,) * 2)
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
        resource.setrlimit(resource.RLIMIT_NPROC, (max_processes, max_processes))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

    return apply
