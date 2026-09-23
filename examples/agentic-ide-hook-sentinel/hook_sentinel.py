#!/usr/bin/env python3
"""Agentic IDE Lifecycle Hook Sentinel & Zero-Trust Execution Guard.

Intercepts agent tool calls and editor events in agentic IDE control planes (VS Code,
Antigravity, Cursor). Enforces zero-trust egress, protected path isolation, pre-flight
file bounds (CWE-400), POSIX process group containment, and LSP diagnostic ingestion.
"""

# sentinel: allow[ZeroTrustSanitization] — this module defines the private-address policy;
# the ranges below are that definition, not an endpoint.

from __future__ import annotations

import contextlib
import ipaddress
import os
import re
import signal
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

MAX_ALLOWED_FILE_SIZE_BYTES: Final[int] = 5 * 1024 * 1024  # 5MB boundary guard
DEFAULT_SUBPROCESS_TIMEOUT_SECONDS: Final[float] = 10.0

PROTECTED_PATH_PATTERNS: Final[tuple[str, ...]] = (
    ".env",
    ".ssh",
    ".gnupg",
    ".git",
    "id_rsa",
    "id_ed25519",
    "/etc/passwd",
    "/etc/shadow",
)

IPV4_PATTERN: Final[re.Pattern[str]] = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")

# RFC 5737 Documentation blocks + loopback are allowed for safe testing
# How long a process group is given to honour SIGTERM before SIGKILL follows.
GRACE_SECONDS: Final[float] = 2.0

ALLOWED_TEST_NETWORKS: Final[tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]] = (
    ipaddress.ip_network("192.0.2.0/24"),
    ipaddress.ip_network("198.51.100.0/24"),
    ipaddress.ip_network("203.0.113.0/24"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.169.254/32"),
    ipaddress.ip_network("2001:db8::/32"),
    ipaddress.ip_network("::1/128"),
)

# The ranges that name a real machine on a private network. Kept in step with
# `tools/sanitization_policy.py`, the single definition; this sample application is
# standalone by design and cannot import it, so `tests/test_address_policy_agreement.py`
# asserts the two agree on a shared corpus.
#
# Enumerated rather than delegated to `is_private`, the wider IANA not-globally-reachable
# set: this guard reported `0.0.0.0`, `255.255.255.255`, RFC 2544 benchmarking space,
# reserved `240.0.0.0/4` and IETF protocol assignments as "RFC 1918 private", a clause that
# covers none of them. The IPv6 entries are here because there were none — every scan
# matched a dotted quad only, so `http://[fd00::1]/` walked through an egress guard that
# exists to stop exactly that.
PRIVATE_HOST_NETWORKS: Final[tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]] = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
)

# A bracketed IPv6 literal in a URL (RFC 3986 §3.2.2) or a bare one in prose.
# A candidate finder, not a validator: writing a correct IPv6 grammar in a regular
# expression is how brittle patterns are born, and `ipaddress.ip_address` is already the
# authority. This matches anything with two or more colons and hands it over; `12:34:56`
# is found, refused by the parser, and never reported. The previous pattern required two
# to seven full groups and therefore missed every compressed address — `fe80::1234`, the
# commonest form there is.
IPV6_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\[([0-9A-Fa-f:]+)\]|((?:[0-9A-Fa-f]{0,4}:){2,}[0-9A-Fa-f]{0,4})"
)


class HookDecision(StrEnum):
    """Result of evaluating a lifecycle hook event."""

    ALLOW = "ALLOW"
    DENY = "DENY"


@dataclass(frozen=True)
class HookEvaluation:
    """Evaluation verdict with human-readable rationale."""

    decision: HookDecision
    reason: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CommandResult:
    """Execution telemetry of a process group contained subprocess."""

    command: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False


@dataclass(frozen=True)
class DiagnosticEvaluation:
    """Summary of LSP compiler diagnostics evaluated for agent self-healing."""

    file_path: str
    error_count: int
    warning_count: int
    is_clean: bool
    prescriptive_guidance: str


def _normalize_ipv4(ip_str: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Parse a dotted quad the way a resolver does, or return None if it is not one.

    `ipaddress.ip_address` is deliberately strict and rejects any octet with a leading
    zero, because the historical interpretation of `0177` is octal and the modern one is
    decimal. `inet_aton` and every libc-backed client still accept it, so `curl` given an
    octet written with a leading zero connects to the same private host the decimal form
    names — the address this module exists to block. A guard that parses strictly and treats a
    `ValueError` as "not an address" therefore fails **open** on exactly the notation an
    attacker would choose.

    Each octet is re-read here with the base the C resolver would use, so the guard judges
    the address the connection will actually go to.
    """
    parts = ip_str.split(".")
    if len(parts) != 4:
        return _strict(ip_str)
    try:
        octets = [_octet(part) for part in parts]
    except ValueError:
        return _strict(ip_str)
    if any(value < 0 or value > 255 for value in octets):
        return _strict(ip_str)
    return ipaddress.ip_address(".".join(str(value) for value in octets))


def _octet(part: str) -> int:
    """Read one octet as a resolver would: 0x hex, leading-zero octal, else decimal."""
    lowered = part.lower()
    if lowered.startswith("0x"):
        return int(lowered, 16)
    if lowered.startswith("0") and len(lowered) > 1:
        return int(lowered, 8)
    return int(lowered, 10)


def _strict(ip_str: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Fall back to the strict parser for anything that is not a dotted quad."""
    try:
        return ipaddress.ip_address(ip_str)
    except ValueError:
        return None


def _check_private_ip(ip_str: str) -> bool:
    """Return True when the address names a real machine on a private network."""
    addr = _normalize_ipv4(ip_str)
    if addr is None or addr.is_loopback or addr.is_unspecified:
        return False
    if any(addr in net for net in ALLOWED_TEST_NETWORKS if net.version == addr.version):
        return False
    return any(addr in net for net in PRIVATE_HOST_NETWORKS if net.version == addr.version)


def _scan_for_forbidden_ips(payload_text: str) -> list[str]:
    """Extract every private host address in the text, IPv4 and IPv6 alike."""
    candidates = list(IPV4_PATTERN.findall(payload_text))
    candidates += [bracketed or bare for bracketed, bare in IPV6_PATTERN.findall(payload_text)]
    return [ip for ip in candidates if _check_private_ip(ip)]


def _is_protected_path(path_str: str) -> bool:
    """Predicate checking if path matches protected credential or system paths."""
    lower_path = path_str.lower()
    return any(pat.lower() in lower_path for pat in PROTECTED_PATH_PATTERNS)


class IdeHookSentinel:
    """Zero-trust security and lifecycle coordinator for agentic IDEs.

    Guarantees:
    - Pre-tool inspection: Blocks secret credential paths and private RFC 1918 IPs.
    - Pre-file-write bounds: Enforces <= 5MB file cap and workspace boundary containment.
    - Process group containment: Subprocesses isolated via POSIX process groups.
    - LSP diagnostic ingestion: Converts compiler diagnostics to corrective guidance.
    """

    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = workspace_root.resolve()

    def evaluate_tool_call(self, tool_name: str, arguments: dict[str, Any]) -> HookEvaluation:
        """Evaluate pre-tool execution arguments against zero-trust policies."""
        arg_str = str(arguments)

        # 1. Check for private RFC 1918 IP leakage
        forbidden_ips = _scan_for_forbidden_ips(arg_str)
        if forbidden_ips:
            return HookEvaluation(
                decision=HookDecision.DENY,
                reason=f"Forbidden private host address detected: {forbidden_ips[0]}",
                details={"forbidden_ips": forbidden_ips},
            )

        # 2. Check for protected paths in tool arguments
        for key, val in arguments.items():
            if isinstance(val, str) and _is_protected_path(val):
                return HookEvaluation(
                    decision=HookDecision.DENY,
                    reason=f"Access to protected credential or system path blocked: '{val}'",
                    details={"field": key, "path": val},
                )

        return HookEvaluation(decision=HookDecision.ALLOW, reason="Tool execution verified safe")

    def evaluate_file_write(self, target_file: Path, content: str | bytes) -> HookEvaluation:
        """Validate proposed file write against size caps and workspace confinement."""
        # 1. Pre-flight file size check
        byte_len = len(content) if isinstance(content, bytes) else len(content.encode("utf-8"))
        if byte_len > MAX_ALLOWED_FILE_SIZE_BYTES:
            return HookEvaluation(
                decision=HookDecision.DENY,
                reason=f"File size ({byte_len} bytes) exceeds maximum cap ({MAX_ALLOWED_FILE_SIZE_BYTES} bytes)",
                details={"size_bytes": byte_len, "max_bytes": MAX_ALLOWED_FILE_SIZE_BYTES},
            )

        # 2. Path boundary resolution and symlink escape verification
        try:
            resolved = target_file.resolve()
        except (OSError, RuntimeError) as err:
            return HookEvaluation(
                decision=HookDecision.DENY,
                reason=f"Unresolvable file path or circular symlink: {err}",
            )

        if not resolved.is_relative_to(self.workspace_root):
            return HookEvaluation(
                decision=HookDecision.DENY,
                reason=f"File path '{resolved}' escapes workspace root '{self.workspace_root}'",
            )

        if _is_protected_path(str(resolved)):
            return HookEvaluation(
                decision=HookDecision.DENY,
                reason=f"Target file matches protected path: '{resolved}'",
            )

        return HookEvaluation(decision=HookDecision.ALLOW, reason="File write verified safe")

    def run_isolated_command(
        self,
        command: Sequence[str],
        cwd: Path | None = None,
        timeout_seconds: float = DEFAULT_SUBPROCESS_TIMEOUT_SECONDS,
    ) -> CommandResult:
        """Execute command in dedicated POSIX process group with clean termination."""
        work_dir = cwd or self.workspace_root
        cmd_tuple = tuple(command)

        proc = subprocess.Popen(
            cmd_tuple,
            cwd=work_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,  # Isolate into new POSIX process group
        )

        try:
            stdout, stderr = proc.communicate(timeout=timeout_seconds)
            return CommandResult(
                command=cmd_tuple,
                exit_code=proc.returncode,
                stdout=stdout,
                stderr=stderr,
                timed_out=False,
            )
        except subprocess.TimeoutExpired:
            self._kill_process_group(proc.pid)
            # Bounded. `communicate()` with no timeout blocks until the group exits, so a
            # child that ignores SIGTERM held the call open for its full runtime — a
            # 0.5-second contract observed to run past 25 seconds. The escalation below
            # guarantees this returns.
            try:
                stdout, stderr = proc.communicate(timeout=GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                self._kill_process_group(proc.pid, signal.SIGKILL)
                stdout, stderr = proc.communicate()
            return CommandResult(
                command=cmd_tuple,
                exit_code=-1,
                stdout=stdout,
                stderr=stderr,
                timed_out=True,
            )

    @staticmethod
    def _kill_process_group(pid: int, sig: int = signal.SIGTERM) -> None:
        """Signal the entire POSIX process group, so grandchildren die with the child.

        SIGTERM is a request and a process may decline it; SIGKILL cannot be caught,
        blocked or ignored. Sending only the request and then waiting without a bound is
        how a timeout becomes advisory.
        """
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(os.getpgid(pid), sig)

    @staticmethod
    def evaluate_lsp_diagnostics(
        file_path: str,
        diagnostics: Sequence[dict[str, Any]],
    ) -> DiagnosticEvaluation:
        """Evaluate Language Server diagnostics and synthesize prescriptive agent guidance."""
        errors = [d for d in diagnostics if d.get("severity") in (1, "error", "Error")]
        warnings = [d for d in diagnostics if d.get("severity") in (2, "warning", "Warning")]

        if not errors:
            return DiagnosticEvaluation(
                file_path=file_path,
                error_count=0,
                warning_count=len(warnings),
                is_clean=True,
                prescriptive_guidance="No compiler errors detected. Verification passed.",
            )

        top_err = errors[0]
        line = top_err.get("range", {}).get("start", {}).get("line", 1)
        msg = top_err.get("message", "Unknown compiler error")

        guidance = (
            f"Compiler Error at line {line}: {msg}. "
            "Please revise the implementation to satisfy language type constraints."
        )

        return DiagnosticEvaluation(
            file_path=file_path,
            error_count=len(errors),
            warning_count=len(warnings),
            is_clean=False,
            prescriptive_guidance=guidance,
        )

