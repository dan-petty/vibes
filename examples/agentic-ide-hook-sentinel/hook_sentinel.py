#!/usr/bin/env python3
"""Agentic IDE Lifecycle Hook Sentinel & Zero-Trust Execution Guard.

Intercepts agent tool calls and editor events in agentic IDE control planes (VS Code,
Antigravity, Cursor). Enforces zero-trust egress, protected path isolation, pre-flight
file bounds (CWE-400), POSIX process group containment, and LSP diagnostic ingestion.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import ipaddress
import os
from pathlib import Path
import re
import signal
import subprocess
from typing import Any, Final, Sequence

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
ALLOWED_TEST_NETWORKS: Final[tuple[ipaddress.IPv4Network, ...]] = (
    ipaddress.ip_network("192.0.2.0/24"),
    ipaddress.ip_network("198.51.100.0/24"),
    ipaddress.ip_network("203.0.113.0/24"),
    ipaddress.ip_network("127.0.0.0/8"),
)


class HookDecision(str, Enum):
    """Result of evaluating a lifecycle hook event."""

    ALLOW = "ALLOW"
    DENY = "DENY"


class LifecycleEventType(str, Enum):
    """Supported agentic IDE lifecycle event types."""

    PRE_TOOL_EXECUTION = "PRE_TOOL_EXECUTION"
    POST_TOOL_EXECUTION = "POST_TOOL_EXECUTION"
    PRE_FILE_WRITE = "PRE_FILE_WRITE"
    ON_LSP_DIAGNOSTICS = "ON_LSP_DIAGNOSTICS"


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


def _check_private_ip(ip_str: str) -> bool:
    """Return True if IP is private RFC 1918 and not an allowed documentation IP."""
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return False

    if not addr.is_private:
        return False

    return not any(addr in net for net in ALLOWED_TEST_NETWORKS)


def _scan_for_forbidden_ips(payload_text: str) -> list[str]:
    """Extract any forbidden RFC 1918 IP addresses from text."""
    matches = IPV4_PATTERN.findall(payload_text)
    return [ip for ip in matches if _check_private_ip(ip)]


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
                reason=f"Forbidden RFC 1918 private IP address detected: {forbidden_ips[0]}",
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
            stdout, stderr = proc.communicate()
            return CommandResult(
                command=cmd_tuple,
                exit_code=-1,
                stdout=stdout,
                stderr=stderr,
                timed_out=True,
            )

    @staticmethod
    def _kill_process_group(pid: int) -> None:
        """Kill entire POSIX process group to prevent orphan grandchild zombie leaks."""
        try:
            pgid = os.getpgid(pid)
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass

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

