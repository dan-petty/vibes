#!/usr/bin/env python3
"""Automated Seccomp BPF Profile Synthesizer.

Generates minimal, tool-specific Linux seccomp-bpf JSON filter profiles based on
static Python AST symbol analysis and syscall trace profiling, restricting agent tool
execution strictly to required system calls.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

SUPPORTED_ARCHITECTURES: Final[tuple[str, ...]] = (
    "SCMP_ARCH_X86_64",
    "SCMP_ARCH_AARCH64",
)

ACT_ERRNO: Final[str] = "SCMP_ACT_ERRNO"
ACT_KILL: Final[str] = "SCMP_ACT_KILL_PROCESS"
ACT_LOG: Final[str] = "SCMP_ACT_LOG"
ACT_ALLOW: Final[str] = "SCMP_ACT_ALLOW"

VALID_ACTIONS: Final[frozenset[str]] = frozenset({
    ACT_ERRNO,
    ACT_KILL,
    ACT_LOG,
    ACT_ALLOW,
})

# Fundamental system calls required by modern Linux x86-64 / arm64 runtimes to
# boot the CPython interpreter, resolve imports, allocate memory, and exit cleanly.
BASELINE_RUNTIME_SYSCALLS: Final[frozenset[str]] = frozenset({
    "access",
    "arch_prctl",
    "brk",
    "clock_gettime",
    "close",
    "dup",
    "dup2",
    "dup3",
    "execve",
    "exit",
    "exit_group",
    "faccessat",
    "faccessat2",
    "fcntl",
    "fstat",
    "futex",
    "getcwd",
    "getdents64",
    "getegid",
    "geteuid",
    "getgid",
    "getpid",
    "getppid",
    "getrandom",
    "gettid",
    "gettimeofday",
    "getuid",
    "ioctl",
    "lseek",
    "mmap",
    "mprotect",
    "munmap",
    "newfstatat",
    "prlimit64",
    "read",
    "readlink",
    "readlinkat",
    "rseq",
    "rt_sigaction",
    "rt_sigprocmask",
    "rt_sigreturn",
    "set_robust_list",
    "set_tid_address",
    "sigaltstack",
    "stat",
    "sysinfo",
    "write",
})

# Module to implied system calls mapping.
MODULE_SYSCALL_MAP: Final[dict[str, tuple[str, ...]]] = {
    "socket": (
        "accept",
        "accept4",
        "bind",
        "connect",
        "getpeername",
        "getsockname",
        "getsockopt",
        "listen",
        "recvfrom",
        "recvmsg",
        "sendmsg",
        "sendto",
        "setsockopt",
        "shutdown",
        "socket",
        "socketpair",
    ),
    "ssl": (
        "poll",
        "recvfrom",
        "recvmsg",
        "sendmsg",
        "sendto",
    ),
    "http": (
        "poll",
        "recvfrom",
        "sendto",
    ),
    "urllib": (
        "poll",
        "recvfrom",
        "sendto",
    ),
    "httpx": (
        "epoll_create1",
        "epoll_ctl",
        "epoll_wait",
        "poll",
        "recvfrom",
        "sendto",
    ),
    "requests": (
        "poll",
        "recvfrom",
        "sendto",
    ),
    "asyncio": (
        "epoll_create1",
        "epoll_ctl",
        "epoll_wait",
        "eventfd2",
        "pipe2",
        "timerfd_create",
        "timerfd_settime",
    ),
    "select": (
        "epoll_create1",
        "epoll_ctl",
        "epoll_wait",
        "poll",
        "ppoll",
        "pselect6",
        "select",
    ),
    "subprocess": (
        "clone",
        "clone3",
        "fork",
        "pipe",
        "pipe2",
        "prctl",
        "vfork",
        "wait4",
        "waitid",
    ),
    "multiprocessing": (
        "clone",
        "clone3",
        "fork",
        "pipe",
        "pipe2",
        "wait4",
    ),
    "signal": (
        "kill",
        "rt_sigpending",
        "rt_sigsuspend",
        "tgkill",
    ),
    "time": (
        "clock_nanosleep",
        "nanosleep",
    ),
    "shutil": (
        "copy_file_range",
        "mkdir",
        "mkdirat",
        "rename",
        "renameat",
        "renameat2",
        "rmdir",
        "unlink",
        "unlinkat",
    ),
    "tempfile": (
        "mkdir",
        "mkdirat",
        "openat",
        "unlink",
        "unlinkat",
    ),
    "sqlite3": (
        "fdatasync",
        "flock",
        "fsync",
        "ftruncate",
        "openat",
    ),
    "threading": (
        "clone",
        "clone3",
        "futex",
    ),
    "posix": (
        "openat",
        "pipe",
        "pipe2",
    ),
}

# Function/method symbol to implied system calls mapping.
SYMBOL_SYSCALL_MAP: Final[dict[str, tuple[str, ...]]] = {
    "open": ("open", "openat"),
    "openat": ("openat",),
    "unlink": ("unlink", "unlinkat"),
    "remove": ("unlink", "unlinkat"),
    "rmdir": ("rmdir",),
    "mkdir": ("mkdir", "mkdirat"),
    "rename": ("rename", "renameat", "renameat2"),
    "replace": ("renameat2",),
    "scandir": ("getdents64",),
    "listdir": ("getdents64",),
    "chmod": ("chmod", "fchmod", "fchmodat"),
    "chown": ("chown", "fchown", "fchownat"),
    "kill": ("kill", "tgkill"),
    "fork": ("fork", "clone", "clone3"),
    "system": ("clone", "clone3", "execve", "wait4"),
    "popen": ("clone", "clone3", "pipe2", "wait4"),
    "connect": ("connect",),
    "bind": ("bind",),
    "listen": ("listen",),
    "accept": ("accept", "accept4"),
    "send": ("sendto", "sendmsg"),
    "recv": ("recvfrom", "recvmsg"),
    "pipe": ("pipe", "pipe2"),
    "symlink": ("symlink", "symlinkat"),
    "readlink": ("readlink", "readlinkat"),
}

# Syscalls posing privilege escalation or sandbox escape danger.
HIGH_RISK_SYSCALLS: Final[frozenset[str]] = frozenset({
    "adjtimex",
    "bpf",
    "chroot",
    "clock_settime",
    "delete_module",
    "finit_module",
    "init_module",
    "ioperm",
    "iopl",
    "kexec_file_load",
    "kexec_load",
    "mount",
    "perf_event_open",
    "pivot_root",
    "process_vm_readv",
    "process_vm_writev",
    "ptrace",
    "reboot",
    "setns",
    "settimeofday",
    "swapoff",
    "swapon",
    "umount2",
    "unshare",
    "userfaultfd",
})

_STRACE_CALL_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^(?:\[pid\s+\d+\]\s+)?([a-zA-Z0-9_]+)\("
)


@dataclass(frozen=True)
class SyscallRule:
    """A single OCI seccomp syscall matching entry."""

    names: tuple[str, ...]
    action: str = ACT_ALLOW
    args: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Serialize rule to standard OCI JSON representation."""
        return {
            "names": list(self.names),
            "action": self.action,
            "args": list(self.args),
        }


@dataclass(frozen=True)
class SeccompProfile:
    """Synthesized OCI seccomp filter specification."""

    default_action: str
    architectures: tuple[str, ...]
    syscalls: tuple[SyscallRule, ...]

    def to_dict(self) -> dict[str, Any]:
        """Serialize profile to standard OCI specification dictionary."""
        return {
            "defaultAction": self.default_action,
            "architectures": list(self.architectures),
            "syscalls": [rule.to_dict() for rule in self.syscalls],
        }

    def to_json(self, indent: int = 2) -> str:
        """Serialize profile to formatted JSON string."""
        return json.dumps(self.to_dict(), indent=indent, sort_keys=False) + "\n"

    def allowed_syscalls(self) -> frozenset[str]:
        """Return the set of all explicitly allowed syscall names."""
        names: set[str] = set()
        for rule in self.syscalls:
            if rule.action == ACT_ALLOW:
                names.update(rule.names)
        return frozenset(names)


@dataclass
class AnalysisResult:
    """Result of static source symbol analysis."""

    modules: set[str] = field(default_factory=set)
    symbols: set[str] = field(default_factory=set)
    syscalls: set[str] = field(default_factory=set)

    def total_syscalls(self) -> frozenset[str]:
        """Compute the combined set of all detected system calls."""
        return frozenset(self.syscalls)


class _AstSymbolVisitor(ast.NodeVisitor):
    """AST visitor extracting imported modules and invoked call symbols."""

    def __init__(self, result: AnalysisResult) -> None:
        self.result = result

    def visit_Import(self, node: ast.Import) -> None:
        """Record imported modules and map them to runtime system calls."""
        for alias in node.names:
            base_module = alias.name.split(".")[0]
            self.result.modules.add(base_module)
            self._record_module_syscalls(base_module)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        """Record from-imported modules and imported symbol names."""
        if node.module:
            base_module = node.module.split(".")[0]
            self.result.modules.add(base_module)
            self._record_module_syscalls(base_module)
        for alias in node.names:
            self.result.symbols.add(alias.name)
            self._record_symbol_syscalls(alias.name)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        """Extract call targets and map resolved function names to system calls."""
        call_name = _resolve_call_name(node.func)
        if call_name:
            self.result.symbols.add(call_name)
            self._record_symbol_syscalls(call_name)
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        """Record attribute accesses and map method names to system calls."""
        self.result.symbols.add(node.attr)
        self._record_symbol_syscalls(node.attr)
        self.generic_visit(node)

    def _record_module_syscalls(self, module_name: str) -> None:
        """Add mapped system calls for recognized standard and third-party modules."""
        if module_name in MODULE_SYSCALL_MAP:
            self.result.syscalls.update(MODULE_SYSCALL_MAP[module_name])

    def _record_symbol_syscalls(self, symbol_name: str) -> None:
        """Add mapped system calls for recognized function and method symbols."""
        if symbol_name in SYMBOL_SYSCALL_MAP:
            self.result.syscalls.update(SYMBOL_SYSCALL_MAP[symbol_name])


def _resolve_call_name(node: ast.AST) -> str | None:
    """Extract bare identifier or trailing attribute from call node."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def analyze_source_code(source: str) -> AnalysisResult:
    """Parse Python source string and extract required syscall set."""
    result = AnalysisResult()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return result
    visitor = _AstSymbolVisitor(result)
    visitor.visit(tree)
    return result


def analyze_path(path: Path) -> AnalysisResult:
    """Recursively analyze Python files in path or inspect single file."""
    combined = AnalysisResult()
    if path.is_file():
        text = path.read_text(encoding="utf-8", errors="replace")
        single = analyze_source_code(text)
        _merge_results(combined, single)
        return combined

    for py_file in path.rglob("*.py"):
        text = py_file.read_text(encoding="utf-8", errors="replace")
        single = analyze_source_code(text)
        _merge_results(combined, single)
    return combined


def _merge_results(target: AnalysisResult, incoming: AnalysisResult) -> None:
    """Merge incoming analysis items into target result container."""
    target.modules.update(incoming.modules)
    target.symbols.update(incoming.symbols)
    target.syscalls.update(incoming.syscalls)


def parse_strace_line(line: str) -> str | None:
    """Extract system call name from strace output line if valid."""
    cleaned = line.strip()
    if not cleaned or cleaned.startswith(("+", "-")):
        return None
    match = _STRACE_CALL_PATTERN.match(cleaned)
    if not match:
        return None
    call_name = match.group(1)
    if call_name.startswith("sys_"):
        call_name = call_name[4:]
    return call_name


def parse_trace_lines(lines: Iterable[str]) -> frozenset[str]:
    """Parse multiple trace lines supporting both strace format and JSON format."""
    detected: set[str] = set()
    for line in lines:
        cleaned = line.strip()
        if not cleaned:
            continue
        parsed = _parse_line_candidate(cleaned)
        if parsed:
            detected.add(parsed)
    return frozenset(detected)


def _parse_json_line(line: str) -> str | None:
    """Attempt to parse a line as a JSON event dictionary."""
    if not (line.startswith("{") and line.endswith("}")):
        return None
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return None
    return _extract_json_syscall(payload) if isinstance(payload, dict) else None


def _parse_line_candidate(line: str) -> str | None:
    """Attempt parsing single line as JSON event or fallback to strace."""
    return _parse_json_line(line) or parse_strace_line(line)


def _extract_json_syscall(payload: dict[str, Any]) -> str | None:
    """Extract syscall identifier from JSON event dictionary."""
    for key in ("syscall", "name", "call"):
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


@dataclass(frozen=True)
class SynthesisConfig:
    """Configuration options for seccomp profile synthesis."""

    default_action: str = ACT_ERRNO
    architectures: tuple[str, ...] = SUPPORTED_ARCHITECTURES
    include_baseline: bool = True


def synthesize_profile(
    detected: Iterable[str],
    config: SynthesisConfig | None = None,
    allow_extra: Iterable[str] = (),
    deny_extra: Iterable[str] = (),
) -> SeccompProfile:
    """Synthesize an OCI seccomp filter specification from required calls."""
    cfg = config or SynthesisConfig()
    if cfg.default_action not in VALID_ACTIONS:
        raise ValueError(
            f"Invalid default action: {cfg.default_action!r}; must be one of {sorted(VALID_ACTIONS)}"
        )

    allowed_set: set[str] = set()
    if cfg.include_baseline:
        allowed_set.update(BASELINE_RUNTIME_SYSCALLS)
    allowed_set.update(detected)
    allowed_set.update(allow_extra)
    allowed_set.difference_update(deny_extra)

    rule = SyscallRule(
        names=tuple(sorted(allowed_set)),
        action=ACT_ALLOW,
    )
    return SeccompProfile(
        default_action=cfg.default_action,
        architectures=cfg.architectures,
        syscalls=(rule,),
    )


def load_profile_from_json(text: str) -> SeccompProfile:
    """Parse an OCI seccomp JSON string into a SeccompProfile instance."""
    data = json.loads(text)
    default_action = data.get("defaultAction", ACT_ERRNO)
    architectures = tuple(data.get("architectures", list(SUPPORTED_ARCHITECTURES)))
    raw_rules = data.get("syscalls", [])
    rules = [
        SyscallRule(
            names=tuple(rule.get("names", ())),
            action=rule.get("action", ACT_ALLOW),
            args=tuple(rule.get("args", ())),
        )
        for rule in raw_rules
    ]
    return SeccompProfile(
        default_action=default_action,
        architectures=architectures,
        syscalls=tuple(rules),
    )


def audit_profile(profile: SeccompProfile) -> list[str]:
    """Audit profile against safety benchmarks and report detected vulnerabilities."""
    findings: list[str] = []
    allowed = profile.allowed_syscalls()

    for dangerous in sorted(allowed & HIGH_RISK_SYSCALLS):
        findings.append(f"HIGH_RISK: Permitted dangerous syscall {dangerous!r}")

    missing_baseline = sorted(BASELINE_RUNTIME_SYSCALLS - allowed)
    if missing_baseline:
        findings.append(
            f"BASELINE_DEFICIT: Missing {len(missing_baseline)} essential runtime syscalls: {missing_baseline[:5]}"
        )

    if profile.default_action == ACT_ALLOW:
        findings.append("INSECURE_DEFAULT: defaultAction is SCMP_ACT_ALLOW (fails open)")

    return findings


def diff_profiles(
    baseline: SeccompProfile,
    candidate: SeccompProfile,
) -> dict[str, Any]:
    """Calculate structural differences between baseline and candidate profiles."""
    base_allowed = baseline.allowed_syscalls()
    cand_allowed = candidate.allowed_syscalls()

    return {
        "action_changed": baseline.default_action != candidate.default_action,
        "base_action": baseline.default_action,
        "cand_action": candidate.default_action,
        "added_syscalls": sorted(cand_allowed - base_allowed),
        "removed_syscalls": sorted(base_allowed - cand_allowed),
    }


def build_arg_parser() -> argparse.ArgumentParser:
    """Build CLI argument parser for seccomp synthesizer."""
    parser = argparse.ArgumentParser(
        description="Automated Seccomp BPF Profile Synthesizer for agent tool execution."
    )
    parser.add_argument(
        "--source",
        "-s",
        type=Path,
        help="Path to Python source file or directory for static symbol analysis.",
    )
    parser.add_argument(
        "--trace",
        "-t",
        type=Path,
        help="Path to strace log file or JSON syscall event stream.",
    )
    parser.add_argument(
        "--out",
        "-o",
        type=Path,
        help="Target output path for the synthesized JSON profile (defaults to stdout).",
    )
    parser.add_argument(
        "--default-action",
        choices=sorted(VALID_ACTIONS),
        default=ACT_ERRNO,
        help="Default action for unmatched system calls (default: SCMP_ACT_ERRNO).",
    )
    parser.add_argument(
        "--allow",
        default="",
        help="Comma-separated list of additional syscalls to permit.",
    )
    parser.add_argument(
        "--deny",
        default="",
        help="Comma-separated list of syscalls to explicitly exclude/deny.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit with non-zero status if any HIGH_RISK syscall is included.",
    )
    parser.add_argument(
        "--audit",
        action="store_true",
        help="Print safety audit findings to stderr.",
    )
    parser.add_argument(
        "--diff",
        type=Path,
        help="Compare synthesized profile against a reference JSON profile file.",
    )
    return parser


def _read_source_syscalls(source_path: Path | None) -> frozenset[str]:
    """Extract syscalls from source path if provided."""
    if not source_path:
        return frozenset()
    return analyze_path(source_path).total_syscalls()


def _read_trace_syscalls(trace_path: Path | None) -> frozenset[str]:
    """Extract syscalls from trace file if present."""
    if not trace_path or not trace_path.is_file():
        return frozenset()
    lines = trace_path.read_text(encoding="utf-8", errors="replace").splitlines()
    return parse_trace_lines(lines)


def _collect_cli_syscalls(args: argparse.Namespace) -> set[str]:
    """Collect detected syscalls from source file and trace inputs."""
    detected: set[str] = set()
    detected.update(_read_source_syscalls(args.source))
    detected.update(_read_trace_syscalls(args.trace))
    return detected


def _split_csv_tokens(raw: str) -> list[str]:
    """Split comma-separated string into non-empty tokens."""
    return [token.strip() for token in raw.split(",") if token.strip()]


def _has_cli_inputs(args: argparse.Namespace) -> bool:
    """Return whether sufficient inputs were provided to the CLI."""
    return bool(args.source or args.trace or args.allow)


def _report_audit(findings: list[str]) -> None:
    """Write audit warning lines to stderr."""
    for item in findings:
        sys.stderr.write(f"[AUDIT] {item}\n")


def _has_strict_violations(findings: list[str]) -> bool:
    """Check for high risk findings under strict enforcement mode."""
    high_risk = [f for f in findings if f.startswith("HIGH_RISK")]
    if high_risk:
        sys.stderr.write(f"Strict audit failed with {len(high_risk)} high risk syscalls.\n")
        return True
    return False


def _emit_profile_output(rendered: str, out_path: Path | None) -> None:
    """Write rendered JSON profile to output file or standard output."""
    if out_path:
        out_path.write_text(rendered, encoding="utf-8")
        return
    sys.stdout.write(rendered)


def _handle_profile_diff(profile: SeccompProfile, diff_path: Path | None) -> None:
    """Compare synthesized profile against reference file and output diff."""
    if not diff_path or not diff_path.is_file():
        return
    text = diff_path.read_text(encoding="utf-8")
    ref_profile = load_profile_from_json(text)
    diff = diff_profiles(ref_profile, profile)
    sys.stderr.write(
        f"[DIFF] Added: {len(diff['added_syscalls'])}, Removed: {len(diff['removed_syscalls'])}\n"
    )


def _build_profile_from_args(args: argparse.Namespace) -> SeccompProfile:
    """Synthesize profile instance from parsed CLI arguments."""
    detected = _collect_cli_syscalls(args)
    allow_extra = _split_csv_tokens(args.allow)
    deny_extra = _split_csv_tokens(args.deny)
    cfg = SynthesisConfig(default_action=args.default_action)
    return synthesize_profile(
        detected,
        config=cfg,
        allow_extra=allow_extra,
        deny_extra=deny_extra,
    )


def _evaluate_cli_reports(
    args: argparse.Namespace,
    profile: SeccompProfile,
    findings: list[str],
) -> bool:
    """Handle audit and diff reporting, returning True if strict violations occurred."""
    if args.audit and findings:
        _report_audit(findings)
    if args.diff:
        _handle_profile_diff(profile, args.diff)
    return bool(args.strict and _has_strict_violations(findings))


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for seccomp profile synthesizer."""
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if not _has_cli_inputs(args):
        parser.print_help(sys.stderr)
        return 2

    profile = _build_profile_from_args(args)
    findings = audit_profile(profile)

    if _evaluate_cli_reports(args, profile, findings):
        return 1

    _emit_profile_output(profile.to_json(), args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())

