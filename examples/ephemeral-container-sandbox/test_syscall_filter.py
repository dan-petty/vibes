"""Tests for kernel-enforced syscall confinement.

Every claim here is executed against the kernel rather than asserted about the program.
A BPF filter that is merely well-formed confines nothing, and the difference is invisible
in its own bytecode — which is the whole reason this module exists.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import syscall_filter as sf

SIGSYS = 31

# Denied by SIGSYS, and chosen because no ordinary workload calls it.
PTRACE_PAYLOAD = (
    "import ctypes, ctypes.util\n"
    "ctypes.CDLL(ctypes.util.find_library('c')).ptrace(0, 0, 0, 0)\n"
    "print('PTRACE SUCCEEDED')\n"
)
# Denied by EPERM, so the workload survives and can report the denial itself.
SOCKET_PAYLOAD = (
    "import socket\n"
    "try:\n"
    "    socket.socket()\n"
    "    print('SOCKET SUCCEEDED')\n"
    "except PermissionError:\n"
    "    print('SOCKET DENIED')\n"
    "print('STILL RUNNING')\n"
)

requires_seccomp = pytest.mark.skipif(
    not sf.supported()[0], reason=f"seccomp unavailable here: {sf.supported()[1]}"
)


def _run(payload: str, groups: tuple[str, ...] = sf.DEFAULT_GROUPS, env: dict[str, str] | None = None
         ) -> subprocess.CompletedProcess[str]:
    """Run a payload in a child confined by the given groups.

    The filter is installed between `fork` and `exec`, so every one of these also proves
    that a seccomp filter is preserved across `execve`: the interpreter that runs the
    payload is not the process the filter was installed in.
    """
    return subprocess.run(
        [sys.executable, "-c", payload],
        capture_output=True, text=True, timeout=60, check=False,
        env=env, preexec_fn=sf.installer(groups),
    )


# --- The table ---------------------------------------------------------------------------


def _kernel_header() -> Path | None:
    """Locate *this* architecture's syscall table, by exact path and never by glob.

    A development host carries cross-compilation headers for every architecture Debian
    supports. `/usr/include/*/asm/unistd_64.h` matched twelve of them here and returned
    aarch64's, whose `accept` is 202 where x86-64's is 43 — so the first version of this
    test reported the committed table as wrong in 45 places. The module's own warning,
    landing on the test written to check it: a syscall number is meaningless without the
    architecture it belongs to.

    `x86_64-linux-gnux32` is excluded for the same reason it is killed in the filter.
    """
    triplet = Path(f"/usr/include/{sf.SUPPORTED_ARCH}-linux-gnu/asm/unistd_64.h")
    native = Path("/usr/include/asm/unistd_64.h")
    return next((path for path in (triplet, native) if path.exists()), None)


def test_every_syscall_number_matches_the_kernels_own_header() -> None:
    """A transcription error here denies a different call than the one named, silently.

    Nothing else would catch it: the filter installs, the program is well-formed, and the
    call the policy meant to deny goes through while some unrelated one is refused.
    """
    header = _kernel_header()
    if header is None:
        pytest.skip("no asm/unistd_64.h on this host; the committed table was NOT cross-checked")
    kernel = {
        match.group(1): int(match.group(2))
        for match in re.finditer(r"#define __NR_(\w+) (\d+)", header.read_text(encoding="utf-8"))
    }
    disagreements = {
        name: (number, kernel.get(name)) for name, number in sf.SYSCALLS.items()
        if kernel.get(name) != number
    }
    assert disagreements == {}


def test_no_two_names_share_a_number() -> None:
    """Runs everywhere, including where the header is absent and the check above skips."""
    assert len(set(sf.SYSCALLS.values())) == len(sf.SYSCALLS)


def test_every_group_resolves_to_known_numbers() -> None:
    """A group naming a syscall the table lacks would confine less than it declares."""
    assert sorted(sf.resolve(tuple(sf.DENY_GROUPS))) == sorted(
        {name for names in sf.DENY_GROUPS.values() for name in names}
    )


def test_an_unknown_group_is_refused_rather_than_ignored() -> None:
    """A typo in a policy must not quietly produce a narrower filter than it asked for."""
    with pytest.raises(sf.SeccompUnavailable, match="unknown deny group"):
        sf.resolve(("netwrok",))


def test_a_name_with_no_number_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """The failure mode is a denylist that silently drops what it could not resolve."""
    monkeypatch.setitem(sf.DENY_GROUPS, "custom", ("no_such_syscall",))
    monkeypatch.setitem(sf.GROUP_ACTIONS, "custom", sf.SECCOMP_RET_KILL_PROCESS)
    with pytest.raises(sf.SeccompUnavailable, match="no syscall number"):
        sf.resolve(("custom",))


def test_killing_outranks_refusing_when_a_call_is_in_two_groups() -> None:
    """Widening a policy must never weaken what an already-denied call does."""
    monkey = {**sf.DENY_GROUPS}
    actions = sf.resolve(("network", "tracing"))
    assert (actions["socket"], actions["ptrace"], "socket" in monkey["network"]) == (
        sf.DENY_EPERM, sf.SECCOMP_RET_KILL_PROCESS, True
    )


# --- The program -------------------------------------------------------------------------


def test_the_program_pins_the_architecture_before_reading_a_call_number() -> None:
    """Call numbers mean different things under different ABIs.

    A filter that compares numbers without pinning `AUDIT_ARCH` denies whatever those
    numbers happen to mean elsewhere, which is a filter that confines nothing it claims to.
    """
    program = sf.build_program(sf.resolve(("tracing",)))
    assert (program[0].k, program[1].k, program[2].k) == (
        4, sf.AUDIT_ARCH_X86_64, sf.SECCOMP_RET_KILL_PROCESS
    )


def test_the_program_kills_the_x32_abi() -> None:
    """The classic bypass: x32 passes the arch check and sets a bit in every call number.

    Without this instruction every comparison misses and every denied call is permitted.
    """
    program = sf.build_program(sf.resolve(("tracing",)))
    assert (program[3].k, program[4].k, program[5].k) == (
        0, sf.X32_SYSCALL_BIT, sf.SECCOMP_RET_KILL_PROCESS
    )


def test_the_program_ends_by_allowing_everything_it_did_not_name() -> None:
    """A denylist that ended in a kill would refuse every call the policy never mentioned."""
    assert sf.build_program(sf.resolve(("tracing",)))[-1].k == sf.SECCOMP_RET_ALLOW


# --- The kernel --------------------------------------------------------------------------


@requires_seccomp
def test_a_denied_call_kills_the_process() -> None:
    """Executed against the kernel: a well-formed filter that does not bite is not a filter."""
    proc = _run(PTRACE_PAYLOAD)
    assert (proc.returncode, "PTRACE SUCCEEDED" in proc.stdout) == (-SIGSYS, False)


@requires_seccomp
def test_a_socket_is_refused_with_an_errno_rather_than_a_death() -> None:
    """The workload has to survive its own denial to be able to report it."""
    proc = _run(SOCKET_PAYLOAD)
    assert (proc.returncode, "SOCKET DENIED" in proc.stdout, "STILL RUNNING" in proc.stdout) == (
        0, True, True
    )


@requires_seccomp
def test_ordinary_work_still_runs_with_the_environment_stripped() -> None:
    """The regression that made `socket` refuse rather than kill.

    The C library resolves users through NSS over a socket, so killing on `socket` killed
    any interpreter started without `HOME`: `site.py` computes the user site directory,
    `expanduser` falls back to `pwd.getpwuid`, and the process died with SIGSYS before
    running a line of the payload. This harness always strips the environment, so it always
    hit it — and the symptom was a sandbox that confined every workload, including the
    benign ones, by killing them.
    """
    proc = _run("print('ordinary work')", env={"PATH": os.environ.get("PATH", "")})
    assert (proc.returncode, proc.stdout.strip()) == (0, "ordinary work")


@requires_seccomp
def test_a_group_left_out_of_the_policy_is_not_denied() -> None:
    """The filter must deny what was asked for and nothing else, or policies mean nothing."""
    proc = _run(PTRACE_PAYLOAD, groups=("network",))
    assert (proc.returncode, "PTRACE SUCCEEDED" in proc.stdout) == (0, True)


def test_unsupported_hosts_say_why_rather_than_reporting_false() -> None:
    """A sandbox that cannot confine must say so; one that says nothing is the defect."""
    ok, reason = sf.supported()
    assert ok or reason != ""
