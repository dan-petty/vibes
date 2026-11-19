"""eBPF Runtime LSM Kernel Gate & Dynamic Syscall Fuzzing Oracle.

Simulates BPF LSM (Linux Security Module) policy decisions in Python. It evaluates
event records for binary executions, sensitive filesystem paths, privilege escalation,
and network egress. It loads no BPF program and intercepts no real syscall. The file
check normalizes `.`, `..` and repeated slashes before it matches; it does not resolve
symbolic links. The probe list comes from the policy's absolute blocked paths plus fixed
evasion, egress and binary probes; it is not syzkaller, and nothing is random. The
Tetragon generator emits a TracingPolicy whose selectors send `Sigkill` for the policy's
blocked binaries, absolute paths and egress. The module ships no BPF C.

Passes the strict sentinel preset (M <= 6, nesting depth <= 3).
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import posixpath
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

GATE_VERSION: Final[str] = "1.0.0"

DISALLOWED_BINARIES: Final[frozenset[str]] = frozenset(
    {"nc", "ncat", "netcat", "socat", "telnet", "chattr", "tcpdump"}
)

DEFAULT_ALLOWED_BINARIES: Final[frozenset[str]] = frozenset(
    {"python", "pytest", "git", "bash", "sh", "ruff", "cargo", "go"}
)

BLOCKED_PATH_PREFIXES: Final[tuple[str, ...]] = (
    "/etc/shadow",
    "/etc/sudoers",
    "/root",
    "~/.ssh",
    "~/.aws",
    ".git/hooks",
    "/proc/kcore",
)

METADATA_ENDPOINT: Final[str] = "169.254.169.254"

LOOPBACK_NETWORKS: Final[tuple[str, ...]] = ("127.0.0.0/8", "::1/128")

TETRAGON_MODE_ANNOTATION: Final[str] = "agent-lsm-gate/mode"

# The generated policy acts only on pods that carry this label, never on host processes.
TETRAGON_POD_LABEL: Final[tuple[str, str]] = ("app", "agent-sandbox")

# Fixed evasion probes, appended to a workspace base path by the probe campaign.
EVASION_PATH_SUFFIXES: Final[tuple[str, ...]] = (
    "/../../etc/shadow",
    "//..//root",
    "/.../etc/sudoers",
    "/\\0.git/hooks",
    "/.git/hooks/pre-commit",
)

# One argument match of a Tetragon selector: argument index, operator and values.
ArgMatch = tuple[int, str, tuple[str, ...]]


class LSMHookType(StrEnum):
    """eBPF LSM hook interception points."""

    BPRM_CHECK = "bprm_check_security"
    FILE_OPEN = "file_open"
    SOCKET_CONNECT = "socket_connect"
    TASK_SETUID = "task_fix_setuid"
    CAPSET = "capset"


class LSMVerdict(StrEnum):
    """Verdict returned by LSM authorization hook."""

    ALLOW = "ALLOW"
    BLOCK = "BLOCK"
    AUDIT = "AUDIT"


@dataclass(frozen=True)
class LSMEvent:
    """Interception event representing an attempted kernel operation."""

    hook: LSMHookType
    comm: str
    target: str
    flags: int = 0
    uid: int = 1000
    pid: int = 1001


@dataclass(frozen=True)
class LSMPolicy:
    """Declared LSM sandbox security policy."""

    allowed_binaries: frozenset[str] = DEFAULT_ALLOWED_BINARIES
    blocked_binaries: frozenset[str] = DISALLOWED_BINARIES
    blocked_paths: tuple[str, ...] = BLOCKED_PATH_PREFIXES
    allow_loopback_only: bool = True
    enforce_syz_bounds: bool = True


@dataclass(frozen=True)
class LSMFinding:
    """Diagnostic finding produced by an LSM authorization evaluation."""

    rule_id: str
    verdict: LSMVerdict
    event: LSMEvent
    reason: str


@dataclass(frozen=True)
class FuzzReport:
    """Summary telemetry produced by the policy-derived probe campaign."""

    total_probes: int
    blocked_probes: int
    allowed_probes: int
    containment_rate: float
    monotonically_contained: bool


def _is_suspicious_evasion(target: str) -> bool:
    """Detect evasion strings such as directory traversal or null bytes."""
    if "\x00" in target or "\\0" in target:
        return True
    return any(p in target for p in ("..//", ".../", "/..", "\\.."))


def _evaluate_evasion(event: LSMEvent) -> LSMFinding | None:
    """Check for evasion attempts such as traversal strings, null bytes and negative flags."""
    if event.flags < 0 or _is_suspicious_evasion(event.target):
        return LSMFinding(
            "LSM005",
            LSMVerdict.BLOCK,
            event,
            f"Adversarial syscall evasion attempt: {event.target}",
        )
    return None


def _evaluate_bprm(event: LSMEvent, policy: LSMPolicy) -> LSMFinding | None:
    """Evaluate binary execution against allowlist and blocklist."""
    comm = event.comm.strip().lower()
    if comm in policy.blocked_binaries:
        return LSMFinding(
            "LSM001",
            LSMVerdict.BLOCK,
            event,
            f"Explicitly blocked binary execution: {comm}",
        )
    if comm and comm not in policy.allowed_binaries:
        return LSMFinding(
            "LSM001",
            LSMVerdict.BLOCK,
            event,
            f"Unauthorized binary outside sandbox allowlist: {comm}",
        )
    return None


def _clean_ip_target(target: str) -> str:
    """Extract host string handling IPv4 and IPv6 bracket notation."""
    clean = target.strip()
    if clean.startswith("[") and "]" in clean:
        return clean[1 : clean.index("]")]
    if clean.count(":") == 1:
        return clean.split(":")[0]
    return clean


def _is_ip_blocked(target: str, allow_loopback_only: bool) -> bool:
    """Check if target IP is disallowed under zero-trust policy."""
    try:
        ip = ipaddress.ip_address(_clean_ip_target(target))
    except ValueError:
        return False
    if ip.is_loopback:
        return False
    if ip.is_private or ip.is_link_local or str(ip) == METADATA_ENDPOINT:
        return True
    return allow_loopback_only


def _evaluate_socket(event: LSMEvent, policy: LSMPolicy) -> LSMFinding | None:
    """Evaluate network socket connections for egress containment."""
    target = event.target.strip()
    if target == METADATA_ENDPOINT or _is_ip_blocked(target, policy.allow_loopback_only):
        return LSMFinding(
            "LSM002",
            LSMVerdict.BLOCK,
            event,
            f"Network egress violation to unauthorized address: {target}",
        )
    return None


def normalize_path(target: str) -> str:
    """Collapse `.`, `..` and repeated slashes the way the kernel resolves a path name.

    `posixpath.normpath` keeps a leading `//` because POSIX lets an implementation
    give it a meaning; Linux does not, so one slash replaces it. Symbolic links are
    not resolved: that needs the filesystem, and the kernel hook sees the resolved file.
    """
    norm = posixpath.normpath(target.strip())
    return "/" + norm.lstrip("/") if norm.startswith("//") else norm


def _evaluate_file(event: LSMEvent, policy: LSMPolicy) -> LSMFinding | None:
    """Evaluate filesystem access against sensitive path rules."""
    norm = normalize_path(event.target)
    for prefix in policy.blocked_paths:
        if norm.startswith(prefix) or prefix in norm:
            return LSMFinding(
                "LSM003",
                LSMVerdict.BLOCK,
                event,
                f"Sensitive filesystem path access blocked: {norm}",
            )
    return None


def _evaluate_privilege(event: LSMEvent, _policy: LSMPolicy) -> LSMFinding | None:
    """Evaluate privilege escalation attempts."""
    if event.uid == 0 or event.target.lower() in ("root", "cap_sys_admin", "cap_net_admin"):
        return LSMFinding(
            "LSM004",
            LSMVerdict.BLOCK,
            event,
            f"Privilege escalation attempt: {event.target}",
        )
    return None


_HOOK_EVALUATORS = {
    LSMHookType.BPRM_CHECK: _evaluate_bprm,
    LSMHookType.SOCKET_CONNECT: _evaluate_socket,
    LSMHookType.FILE_OPEN: _evaluate_file,
    LSMHookType.TASK_SETUID: _evaluate_privilege,
    LSMHookType.CAPSET: _evaluate_privilege,
}


def evaluate_event(event: LSMEvent, policy: LSMPolicy | None = None) -> LSMFinding:
    """Authoritatively evaluate an LSM event against the active policy."""
    pol = policy or LSMPolicy()
    evasion = _evaluate_evasion(event)
    if evasion is not None:
        return evasion
    evaluator = _HOOK_EVALUATORS.get(event.hook)
    if evaluator is not None:
        finding = evaluator(event, pol)
        if finding is not None:
            return finding
    return LSMFinding("LSM000", LSMVerdict.ALLOW, event, "Permitted by simulated LSM policy")


def _path_spellings(path: str) -> list[str]:
    """Return a path and its dot, double-slash and parent-directory spellings."""
    head, tail = posixpath.split(normalize_path(path))
    head = head.rstrip("/")
    parent = posixpath.basename(head)
    climb = f"{head}/../{parent}/{tail}" if parent else f"/../{tail}"
    return [f"{head}/{tail}", f"{head}/./{tail}", f"{head}//{tail}", climb]


def generate_syz_path_mutations(policy: LSMPolicy | None = None, base: str = "/workspace") -> list[str]:
    """Derive path probes from the policy's absolute blocked paths, then add the evasion probes."""
    pol = policy or LSMPolicy()
    spellings = [s for p in pol.blocked_paths if p.startswith("/") for s in _path_spellings(p)]
    return [*spellings, *(base + suffix for suffix in EVASION_PATH_SUFFIXES)]


def generate_syz_socket_mutations() -> list[str]:
    """Generate network egress mutations including metadata and private addresses."""
    priv_a = str(ipaddress.IPv4Address(int.from_bytes(b"\n\x00\x00\x01", "big")))
    priv_b = str(ipaddress.IPv4Address(int.from_bytes(b"\xac\x10\x00\x01", "big")))
    priv_c = str(ipaddress.IPv4Address(int.from_bytes(b"\xc0\xa8\x01\x01", "big")))
    return [
        METADATA_ENDPOINT,
        priv_a,
        priv_b,
        priv_c,
        "fd00::1",
        "127.0.0.1",
    ]


def _collect_fuzz_events(policy: LSMPolicy) -> list[LSMEvent]:
    """Collect test events across all hooks."""
    ev_files = [LSMEvent(LSMHookType.FILE_OPEN, "runner", p) for p in generate_syz_path_mutations(policy)]
    ev_sockets = [LSMEvent(LSMHookType.SOCKET_CONNECT, "runner", a) for a in generate_syz_socket_mutations()]
    ev_bins = [LSMEvent(LSMHookType.BPRM_CHECK, b, f"/bin/{b}") for b in ("nc", "socat", "unknown_compiler")]
    ev_priv = [LSMEvent(LSMHookType.TASK_SETUID, "runner", "root", uid=0)]
    return [*ev_files, *ev_sockets, *ev_bins, *ev_priv]


def run_fuzz_campaign(policy: LSMPolicy | None = None, seed: int = 42) -> FuzzReport:
    """Evaluate the policy-derived probe list against the policy; `seed` is unused."""
    pol = policy or LSMPolicy()
    events = _collect_fuzz_events(pol)
    blocked = sum(1 for e in events if evaluate_event(e, pol).verdict == LSMVerdict.BLOCK)
    total = len(events)
    allowed = total - blocked
    contained = allowed <= 1
    rate = round(blocked / total, 3) if total > 0 else 1.0
    return FuzzReport(total, blocked, allowed, rate, contained)


def _egress_matches(policy: LSMPolicy) -> list[ArgMatch]:
    """Match IPv4 and IPv6 connects whose destination the policy blocks.

    For a `sockaddr` argument, Tetragon reads the connect address into its `SAddr` field.
    """
    family: ArgMatch = (1, "Family", ("AF_INET", "AF_INET6"))
    if policy.allow_loopback_only:
        return [family, (1, "NotSAddr", LOOPBACK_NETWORKS)]
    link_local = str(ipaddress.ip_network(f"{METADATA_ENDPOINT}/16", strict=False))
    return [family, (1, "SAddr", (link_local,))]


def _has_values(matches: Sequence[ArgMatch]) -> bool:
    """Return True when every argument match has at least one value."""
    return all(values for _, _, values in matches)


def _tetragon_kprobes(policy: LSMPolicy) -> list[tuple[str, tuple[tuple[int, str], ...], list[ArgMatch]]]:
    """Map the policy to kprobes; drop a kprobe that has no value to match."""
    binaries = tuple(f"/{b}" for b in sorted(policy.blocked_binaries))
    paths = tuple(p for p in policy.blocked_paths if p.startswith("/"))
    kprobes = [
        ("security_bprm_check", ((0, "linux_binprm"),), [(0, "Postfix", binaries)]),
        ("security_file_open", ((0, "file"),), [(0, "Prefix", paths)]),
        ("security_socket_connect", ((0, "socket"), (1, "sockaddr")), _egress_matches(policy)),
    ]
    return [k for k in kprobes if _has_values(k[2])]


def _yaml_kprobe(call: str, args: Sequence[tuple[int, str]], matches: Sequence[ArgMatch]) -> list[str]:
    """Render one kprobe whose single selector sends Sigkill when every match holds."""
    lines = [f"    - call: {json.dumps(call)}", "      syscall: false", "      args:"]
    for index, arg_type in args:
        lines += [f"        - index: {index}", f"          type: {json.dumps(arg_type)}"]
    lines += ["      selectors:", "        - matchArgs:"]
    for index, operator, values in matches:
        lines += [f"            - index: {index}", f"              operator: {json.dumps(operator)}", "              values:"]
        lines += [f"                - {json.dumps(value)}" for value in values]
    return [*lines, "          matchActions:", "            - action: Sigkill"]


def generate_tetragon_policy(policy: LSMPolicy | None = None) -> str:
    """Emit a Cilium Tetragon TracingPolicy that sends Sigkill for the policy's blocked operations.

    It covers the blocked binaries, the absolute blocked paths and egress. The binary
    allowlist, relative paths and the privilege and evasion rules stay simulation-only.
    """
    pol = policy or LSMPolicy()
    label_key, label_value = TETRAGON_POD_LABEL
    lines = [
        "apiVersion: cilium.io/v1alpha1",
        "kind: TracingPolicy",
        "metadata:",
        "  name: agent-lsm-sandbox-gate",
        "  annotations:",
        f"    {TETRAGON_MODE_ANNOTATION}: enforce",
        "spec:",
        "  podSelector:",
        "    matchLabels:",
        f"      {label_key}: {json.dumps(label_value)}",
        "  kprobes:",
    ]
    for call, args, matches in _tetragon_kprobes(pol):
        lines += _yaml_kprobe(call, args, matches)
    return "\n".join(lines) + "\n"


def format_sarif(findings: Sequence[LSMFinding]) -> dict[str, Any]:
    """Format LSM findings as OASIS SARIF 2.1.0 telemetry."""
    rules = [
        {"id": "LSM001", "name": "UnauthorizedBinaryExecution", "shortDescription": {"text": "Binary execution outside allowlist"}},
        {"id": "LSM002", "name": "NetworkEgressViolation", "shortDescription": {"text": "Socket connect to unauthorized address"}},
        {"id": "LSM003", "name": "SensitivePathAccess", "shortDescription": {"text": "Filesystem path access blocked"}},
        {"id": "LSM004", "name": "PrivilegeEscalation", "shortDescription": {"text": "Unauthorized setuid or privilege claim"}},
        {"id": "LSM005", "name": "AdversarialEvasion", "shortDescription": {"text": "Syscall parameter evasion detected"}},
    ]
    results = []
    for f in findings:
        if f.verdict == LSMVerdict.BLOCK:
            results.append({
                "ruleId": f.rule_id,
                "level": "error",
                "message": {"text": f.reason},
                "locations": [{"physicalLocation": {"artifactLocation": {"uri": f.event.comm}}}],
            })
    return {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [{
            "tool": {"driver": {"name": "lsm_kernel_gate", "version": GATE_VERSION, "rules": rules}},
            "results": results,
        }],
    }


def _build_parser() -> argparse.ArgumentParser:
    """Build CLI argument parser."""
    parser = argparse.ArgumentParser(description="eBPF Runtime LSM Kernel Gate Oracle")
    sub = parser.add_subparsers(dest="command", required=True)

    eval_p = sub.add_parser("eval", help="Evaluate a single LSM event")
    eval_p.add_argument("--hook", type=str, default="file_open", choices=[h.value for h in LSMHookType])
    eval_p.add_argument("--comm", type=str, default="runner")
    eval_p.add_argument("--target", type=str, required=True)
    eval_p.add_argument("--flags", type=int, default=0)
    eval_p.add_argument("--uid", type=int, default=1000)

    fuzz_p = sub.add_parser("fuzz", help="Run the policy-derived boundary probes against the simulated policy")
    fuzz_p.add_argument("--seed", type=int, default=42)

    gen_p = sub.add_parser("generate", help="Print a Tetragon TracingPolicy that enforces the policy")
    gen_p.add_argument("--format", type=str, default="tetragon", choices=["tetragon"])

    sarif_p = sub.add_parser("sarif", help="Evaluate event and export as SARIF")
    sarif_p.add_argument("--target", type=str, required=True)
    sarif_p.add_argument("--hook", type=str, default="file_open")
    sarif_p.add_argument("--comm", type=str, default="runner")
    return parser


def _handle_eval(args: argparse.Namespace, policy: LSMPolicy) -> int:
    ev = LSMEvent(LSMHookType(args.hook), args.comm, args.target, args.flags, args.uid)
    res = evaluate_event(ev, policy)
    print(f"[{res.verdict.value}] Rule: {res.rule_id} — {res.reason}")
    return 1 if res.verdict == LSMVerdict.BLOCK else 0


def _handle_fuzz(args: argparse.Namespace, policy: LSMPolicy) -> int:
    rep = run_fuzz_campaign(policy, args.seed)
    print(f"Fuzz Probes: {rep.total_probes} | Blocked: {rep.blocked_probes} | Rate: {rep.containment_rate}")
    print(f"Monotonically Contained: {rep.monotonically_contained}")
    return 0 if rep.monotonically_contained else 1


def _handle_generate(_args: argparse.Namespace, policy: LSMPolicy) -> int:
    print(generate_tetragon_policy(policy), end="")
    return 0


def _handle_sarif(args: argparse.Namespace, policy: LSMPolicy) -> int:
    ev = LSMEvent(LSMHookType(args.hook), args.comm, args.target)
    res = evaluate_event(ev, policy)
    sarif = format_sarif([res])
    print(json.dumps(sarif, indent=2))
    return 0


_DISPATCH_HANDLERS = {
    "eval": _handle_eval,
    "fuzz": _handle_fuzz,
    "generate": _handle_generate,
    "sarif": _handle_sarif,
}


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for eBPF LSM Gate Oracle."""
    args = _build_parser().parse_args(argv)
    handler = _DISPATCH_HANDLERS.get(args.command)
    if handler is not None:
        return handler(args, LSMPolicy())
    return 0


if __name__ == "__main__":
    sys.exit(main())
