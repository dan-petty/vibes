"""eBPF Process Tracing & Sandbox Introspection Oracle.

Audits JSON event records (one object per line) for unauthorized process execution,
network egress violations, sensitive file access, privilege escalation, and anti-debugging
evasion, and exports findings to OASIS SARIF 2.1.0. It attaches no eBPF program and traces
nothing. A record is a flat object (keys such as `event_type`, `comm`, `path`, `daddr`) or
a native Tetragon `process_exec` record. Other native Tetragon records, such as
`process_kprobe`, and Falco JSON become `unknown` events; convert them to the flat form first.

The sensitive-path check expands each `~/` entry below the configured home directories and
compares whole path components. The Tetragon TracingPolicy has one `sys_execve` syscall
kprobe that sends `Sigkill` when the executed path names a disallowed binary. Its
`podSelector` limits it to the pods with the sandbox label (`--pod-label`). The observed
binaries go into a metadata annotation; the policy does not enforce them. The Falco rule
only alerts.

Maintains proactive complexity headroom (M <= 6, depth <= 3, parameters <= 4).
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import posixpath
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from functools import partial
from pathlib import Path, PurePosixPath
from typing import Any, Final

TRACER_VERSION: Final[str] = "1.0.0"

DISALLOWED_BINARIES: Final[frozenset[str]] = frozenset(
    {
        "nc",
        "ncat",
        "netcat",
        "telnet",
        "nmap",
        "socat",
        "tcpdump",
        "wireshark",
        "chattr",
    }
)

SENSITIVE_PATH_PREFIXES: Final[tuple[str, ...]] = (
    "/etc/shadow",
    "/etc/passwd",
    "/etc/sudoers",
    "/root",
    "~/.ssh",
    "~/.aws",
    "~/.gnupg",
    "~/.config/gcloud",
    "/proc/kcore",
)

# Kernel events carry absolute paths, so each `~/` entry is written below these directories.
DEFAULT_HOME_DIRS: Final[tuple[str, ...]] = ("/root",)

# The observed binaries are a baseline for review. The TracingPolicy CRD has no field for
# them, and the policy does not enforce them, so they go into this metadata annotation.
TETRAGON_BASELINE_ANNOTATION: Final[str] = "ebpf-tracer/observed-binaries"

# The label that resources/k8s/agent-sandbox/sandbox-pod.yaml puts on the sandbox pod. The
# policy's podSelector uses it, so the Sigkill reaches sandbox pods only.
TETRAGON_POD_LABEL: Final[tuple[str, str]] = ("app.kubernetes.io/name", "agent-sandbox")

# Kernel system calls only: `cap_set_proc` is the libcap function that calls `capset`.
PRIVILEGE_SYSCALLS: Final[frozenset[str]] = frozenset(
    {
        "setuid",
        "setgid",
        "capset",
        "pivot_root",
        "unshare",
    }
)

SARIF_RULES: Final[dict[str, dict[str, Any]]] = {
    "EBPF001": {
        "id": "EBPF001",
        "name": "UnauthorizedBinaryExecution",
        "shortDescription": {
            "text": "Agent executed an unauthorized utility or shell outside the sandbox manifest."
        },
        "defaultConfiguration": {"level": "error"},
        "helpUri": "https://github.com/dan-petty/vibes",
    },
    "EBPF002": {
        "id": "EBPF002",
        "name": "NetworkEgressViolation",
        "shortDescription": {
            "text": "Outbound socket connection directed to private RFC 1918 or cloud metadata space."
        },
        "defaultConfiguration": {"level": "error"},
        "helpUri": "https://github.com/dan-petty/vibes",
    },
    "EBPF003": {
        "id": "EBPF003",
        "name": "SensitiveFileTampering",
        "shortDescription": {
            "text": "Process attempted to access or modify host files outside workspace sandbox root."
        },
        "defaultConfiguration": {"level": "error"},
        "helpUri": "https://github.com/dan-petty/vibes",
    },
    "EBPF004": {
        "id": "EBPF004",
        "name": "PrivilegeEscalationAttempt",
        "shortDescription": {
            "text": "Process invoked namespace or capability modification syscalls to escalate privileges."
        },
        "defaultConfiguration": {"level": "error"},
        "helpUri": "https://github.com/dan-petty/vibes",
    },
    "EBPF005": {
        "id": "EBPF005",
        "name": "AntiDebuggingEvasion",
        "shortDescription": {
            "text": "Process attempted ptrace or memory introspection evasion against runtime audit."
        },
        "defaultConfiguration": {"level": "warning"},
        "helpUri": "https://github.com/dan-petty/vibes",
    },
}


class EbpfEventType(StrEnum):
    """Categorized eBPF event types captured from kernel hooks."""

    EXECVE = "execve"
    SOCKET_CONNECT = "socket_connect"
    FILE_OPEN = "file_open"
    PRIVILEGE_CHANGE = "privilege_change"
    PTRACE_EVASION = "ptrace_evasion"
    UNKNOWN = "unknown"


class EbpfSeverity(StrEnum):
    """Severity classification for runtime eBPF security violations."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class TracerPreset(StrEnum):
    """Evaluation sensitivity preset for eBPF auditing."""

    STRICT = "strict"
    AUDIT_ONLY = "audit_only"
    NETWORK_ONLY = "network_only"


@dataclass(frozen=True)
class EbpfTraceEvent:
    """Normalized eBPF telemetry event captured from kernel tracepoints or LSM."""

    event_type: EbpfEventType
    pid: int
    process_name: str
    command_line: str = ""
    target_path: str = ""
    destination_ip: str = ""
    destination_port: int = 0
    syscall: str = ""
    timestamp: float = 0.0
    namespace_id: int = 0


@dataclass(frozen=True)
class EbpfFinding:
    """Specific security violation detected in eBPF execution stream."""

    rule_id: str
    severity: EbpfSeverity
    message: str
    process: str
    pid: int
    detail: str
    recommendation: str
    finding_hash: str


@dataclass(frozen=True)
class EbpfAuditSummary:
    """Consolidated summary of eBPF trace analysis."""

    total_events: int
    total_violations: int
    is_compliant: bool
    findings: tuple[EbpfFinding, ...]
    findings_by_rule: dict[str, int]


def _compute_hash(content: str) -> str:
    """Return SHA-256 digest of finding signature."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


def _parse_tetragon_exec(raw: dict[str, Any]) -> EbpfTraceEvent:
    """Map a native Tetragon `process_exec` record to an execve event.

    Tetragon writes the JSON field names of its protobuf API (`process_exec.process.binary`,
    `arguments`, `pid`) and an RFC 3339 `time`. `binary` is the executed path, and
    `arguments` holds the arguments without it.
    """
    process = raw["process_exec"].get("process", {})
    binary = str(process.get("binary", ""))
    event_time = str(raw.get("time", ""))
    return EbpfTraceEvent(
        event_type=EbpfEventType.EXECVE,
        pid=int(process.get("pid", 0)),
        process_name=PurePosixPath(binary).name or "unknown",
        command_line=f"{binary} {process.get('arguments', '')}".strip(),
        target_path=binary,
        syscall="execve",
        timestamp=datetime.fromisoformat(event_time).timestamp() if event_time else 0.0,
    )


def parse_trace_event(raw: dict[str, Any]) -> EbpfTraceEvent:
    """Parse a flat record or a native Tetragon `process_exec` record into an EbpfTraceEvent."""
    if "process_exec" in raw:
        return _parse_tetragon_exec(raw)
    raw_type = str(raw.get("event_type", raw.get("type", "unknown"))).lower()
    event_type = _resolve_event_type(raw_type)

    return EbpfTraceEvent(
        event_type=event_type,
        pid=int(raw.get("pid", 0)),
        process_name=str(raw.get("process_name", raw.get("comm", "unknown"))),
        command_line=str(raw.get("command_line", raw.get("args", ""))),
        target_path=str(raw.get("target_path", raw.get("path", ""))),
        destination_ip=str(raw.get("destination_ip", raw.get("daddr", ""))),
        destination_port=int(raw.get("destination_port", raw.get("dport", 0))),
        syscall=str(raw.get("syscall", "")),
        timestamp=float(raw.get("timestamp", 0.0)),
        namespace_id=int(raw.get("namespace_id", raw.get("ns", 0))),
    )


def _resolve_event_type(raw_type: str) -> EbpfEventType:
    """Map string representation to EbpfEventType enum."""
    mapping = {
        "execve": EbpfEventType.EXECVE,
        "process_exec": EbpfEventType.EXECVE,
        "socket_connect": EbpfEventType.SOCKET_CONNECT,
        "connect": EbpfEventType.SOCKET_CONNECT,
        "file_open": EbpfEventType.FILE_OPEN,
        "openat": EbpfEventType.FILE_OPEN,
        "privilege_change": EbpfEventType.PRIVILEGE_CHANGE,
        "ptrace_evasion": EbpfEventType.PTRACE_EVASION,
    }
    return mapping.get(raw_type, EbpfEventType.UNKNOWN)


def parse_events_jsonl(text: str) -> list[EbpfTraceEvent]:
    """Parse newline-delimited JSON stream of eBPF events."""
    events: list[EbpfTraceEvent] = []
    for line in text.splitlines():
        line_s = line.strip()
        if not line_s or line_s.startswith("#"):
            continue
        data = json.loads(line_s)
        events.append(parse_trace_event(data))
    return events


def _check_ebpf001_binary(event: EbpfTraceEvent) -> EbpfFinding | None:
    """Detect execution of disallowed or suspicious binaries."""
    if event.event_type != EbpfEventType.EXECVE:
        return None

    proc_base = Path(event.process_name).name.lower()
    is_disallowed = proc_base in DISALLOWED_BINARIES or "bash -i" in event.command_line
    if not is_disallowed:
        return None

    h = _compute_hash(f"EBPF001:{event.pid}:{event.process_name}")
    return EbpfFinding(
        rule_id="EBPF001",
        severity=EbpfSeverity.CRITICAL,
        message=f"Unauthorized binary execution '{event.process_name}' detected",
        process=event.process_name,
        pid=event.pid,
        detail=f"Command: {event.command_line}",
        recommendation="Pin container allowed binaries and enforce in-kernel Sigkill via Tetragon",
        finding_hash=h,
    )


def _is_prohibited_ip(ip_str: str) -> bool:
    """Check if destination IP is private RFC 1918 or cloud metadata."""
    if not ip_str:
        return False
    ip = ipaddress.ip_address(ip_str)
    return ip.is_private or ip.is_link_local or str(ip) == "169.254.169.254"


def _check_ebpf002_network(event: EbpfTraceEvent) -> EbpfFinding | None:
    """Detect network connections targeting private infrastructure or metadata."""
    if event.event_type != EbpfEventType.SOCKET_CONNECT:
        return None

    if not _is_prohibited_ip(event.destination_ip):
        return None

    h = _compute_hash(f"EBPF002:{event.pid}:{event.destination_ip}:{event.destination_port}")
    return EbpfFinding(
        rule_id="EBPF002",
        severity=EbpfSeverity.HIGH,
        message=f"Prohibited network connection to '{event.destination_ip}:{event.destination_port}'",
        process=event.process_name,
        pid=event.pid,
        detail=f"Syscall: {event.syscall}, Dst: {event.destination_ip}:{event.destination_port}",
        recommendation="Apply zero-trust NetworkPolicy dropping private RFC 1918 and metadata egress",
        finding_hash=h,
    )


def _normalize_path(path: str) -> str:
    """Collapse `.`, `..` and repeated slashes the way the kernel resolves a path name.

    `posixpath.normpath` keeps a leading `//`; Linux gives it no meaning, so one slash
    replaces it. Symbolic links are not resolved.
    """
    norm = posixpath.normpath(path.strip())
    return "/" + norm.lstrip("/") if norm.startswith("//") else norm


def _expand_home(entry: str, home_dirs: Sequence[str]) -> list[str]:
    """Write a `~/...` entry once below each home directory; keep any other entry.

    `posixpath.expanduser` is not used: it reads the home of the user who runs this
    module, not the home of the sandbox user whose events are audited.
    """
    if not entry.startswith("~/"):
        return [entry]
    return [_normalize_path(home + entry[1:]) for home in home_dirs]


def resolve_sensitive_paths(home_dirs: Sequence[str] = DEFAULT_HOME_DIRS) -> tuple[str, ...]:
    """Return the sensitive paths with each `~/` entry expanded below each home directory."""
    expanded = [p for entry in SENSITIVE_PATH_PREFIXES for p in _expand_home(entry, home_dirs)]
    return tuple(dict.fromkeys(expanded))


def _is_under(path: str, entry: str) -> bool:
    """Return True when `path` is `entry` or lies below it, compared by whole path components.

    `/root` covers `/root/.ssh` but not `/rootfs`. A relative path is never under an
    absolute entry.
    """
    return PurePosixPath(_normalize_path(path)).is_relative_to(entry)


def _check_ebpf003_filesystem(event: EbpfTraceEvent, sensitive_paths: Sequence[str]) -> EbpfFinding | None:
    """Detect a process that opens a sensitive host path or a path below it."""
    if event.event_type != EbpfEventType.FILE_OPEN:
        return None

    if not any(_is_under(event.target_path, p) for p in sensitive_paths):
        return None

    h = _compute_hash(f"EBPF003:{event.pid}:{event.target_path}")
    return EbpfFinding(
        rule_id="EBPF003",
        severity=EbpfSeverity.CRITICAL,
        message=f"Sensitive file access attempted on '{event.target_path}'",
        process=event.process_name,
        pid=event.pid,
        detail=f"Target path: {event.target_path}",
        recommendation="Confine container root filesystem with read-only rootfs and restricted volume mounts",
        finding_hash=h,
    )


def _check_ebpf004_privilege(event: EbpfTraceEvent) -> EbpfFinding | None:
    """Detect unauthorized privilege change or namespace manipulation syscalls."""
    is_priv = event.event_type == EbpfEventType.PRIVILEGE_CHANGE or event.syscall in PRIVILEGE_SYSCALLS
    if not is_priv:
        return None

    h = _compute_hash(f"EBPF004:{event.pid}:{event.syscall}")
    return EbpfFinding(
        rule_id="EBPF004",
        severity=EbpfSeverity.HIGH,
        message=f"Unauthorized privilege escalation syscall '{event.syscall}'",
        process=event.process_name,
        pid=event.pid,
        detail=f"Syscall: {event.syscall}, Process: {event.process_name}",
        recommendation="Enable --cap-drop ALL and set securityContext.allowPrivilegeEscalation=false",
        finding_hash=h,
    )


def _check_ebpf005_evasion(event: EbpfTraceEvent) -> EbpfFinding | None:
    """Detect ptrace anti-debugging or memory tampering evasion attempts."""
    is_evasion = event.event_type == EbpfEventType.PTRACE_EVASION or "ptrace" in event.syscall.lower()
    if not is_evasion:
        return None

    h = _compute_hash(f"EBPF005:{event.pid}:{event.syscall}")
    return EbpfFinding(
        rule_id="EBPF005",
        severity=EbpfSeverity.HIGH,
        message=f"Anti-debugging or ptrace evasion detected via '{event.syscall}'",
        process=event.process_name,
        pid=event.pid,
        detail=f"Command: {event.command_line}",
        recommendation="Block ptrace in seccomp filter and verify eBPF LSM hook integrity",
        finding_hash=h,
    )


class EbpfSandboxTracer:
    """Kernel-level eBPF event auditor and policy synthesis engine."""

    def __init__(
        self,
        preset: TracerPreset = TracerPreset.STRICT,
        home_dirs: Sequence[str] = DEFAULT_HOME_DIRS,
    ) -> None:
        """Initialize tracer with a sensitivity preset and the home directories of the `~/` entries."""
        self.preset = preset
        self.sensitive_paths = resolve_sensitive_paths(home_dirs)

    def audit_event(self, event: EbpfTraceEvent) -> list[EbpfFinding]:
        """Audit single eBPF trace event against applicable rules."""
        findings: list[EbpfFinding] = []

        if self.preset == TracerPreset.NETWORK_ONLY:
            f_net = _check_ebpf002_network(event)
            return [f_net] if f_net else []

        checks = (
            _check_ebpf001_binary,
            _check_ebpf002_network,
            partial(_check_ebpf003_filesystem, sensitive_paths=self.sensitive_paths),
            _check_ebpf004_privilege,
            _check_ebpf005_evasion,
        )
        for check in checks:
            f = check(event)
            if f:
                findings.append(f)
        return findings

    def audit_events(self, events: Sequence[EbpfTraceEvent]) -> EbpfAuditSummary:
        """Audit collection of eBPF trace events and compile summary."""
        all_findings: list[EbpfFinding] = []
        rule_counts: dict[str, int] = {}

        for event in events:
            for f in self.audit_event(event):
                all_findings.append(f)
                rule_counts[f.rule_id] = rule_counts.get(f.rule_id, 0) + 1

        is_compliant = not any(f.severity in (EbpfSeverity.CRITICAL, EbpfSeverity.HIGH) for f in all_findings)
        return EbpfAuditSummary(
            total_events=len(events),
            total_violations=len(all_findings),
            is_compliant=is_compliant,
            findings=tuple(all_findings),
            findings_by_rule=rule_counts,
        )


def generate_tetragon_policy(
    events: Sequence[EbpfTraceEvent],
    policy_name: str = "agent-sandbox-containment",
    pod_label: tuple[str, str] = TETRAGON_POD_LABEL,
) -> dict[str, Any]:
    """Synthesize a Tetragon TracingPolicy that kills a process when it executes a disallowed binary.

    The `sys_execve` syscall kprobe reads the path that the caller gives (argument 0). It
    sends `Sigkill` when the path is a disallowed binary name or ends in `/<name>`, the
    same binaries that EBPF001 flags. Each operator has its own selector: a selector
    matches only when all of its matches hold, and the kprobe acts when any selector
    matches. The kprobe does not hook `execveat`. The `podSelector` limits the policy to
    the pods with `pod_label`; without it, Tetragon applies the policy to every process
    on the node. The observed binaries go into the `TETRAGON_BASELINE_ANNOTATION`
    annotation for review; the policy does not enforce them.
    """
    observed = sorted({e.process_name for e in events if e.event_type == EbpfEventType.EXECVE})
    binaries = sorted(DISALLOWED_BINARIES)
    matches = (("Equal", binaries), ("Postfix", [f"/{b}" for b in binaries]))
    label_key, label_value = pod_label
    return {
        "apiVersion": "cilium.io/v1alpha1",
        "kind": "TracingPolicy",
        "metadata": {"name": policy_name, "annotations": {TETRAGON_BASELINE_ANNOTATION: ",".join(observed)}},
        "spec": {
            "podSelector": {"matchLabels": {label_key: label_value}},
            "kprobes": [
                {
                    "call": "sys_execve",
                    "syscall": True,
                    "args": [{"index": 0, "type": "string"}],
                    "selectors": [
                        {
                            "matchArgs": [{"index": 0, "operator": operator, "values": values}],
                            "matchActions": [{"action": "Sigkill"}],
                        }
                        for operator, values in matches
                    ],
                }
            ],
        },
    }


def generate_falco_policy(
    events: Sequence[EbpfTraceEvent],
    rule_name: str = "agent_sandbox_least_privilege",
) -> str:
    """Synthesize Falco YAML rules file based on observed execution trace."""
    allowed = (
        ", ".join(f"'{e.process_name}'" for e in events if e.event_type == EbpfEventType.EXECVE) or "'python'"
    )
    lines = [
        f"- rule: {rule_name}",
        "  desc: Detect unexpected agent process spawns outside baseline manifest",
        f"  condition: spawned_process and not proc.name in ({allowed})",
        '  output: "Unexpected agent process execution (proc=%proc.name cmd=%proc.cmdline)"',
        "  priority: WARNING",
        "  tags: [agent, sandbox, ebpf]",
        "",
    ]
    return "\n".join(lines)


def export_sarif(summary: EbpfAuditSummary, target_uri: str) -> dict[str, Any]:
    """Export eBPF findings as schema-valid OASIS SARIF 2.1.0 dictionary."""
    results: list[dict[str, Any]] = []
    for f in summary.findings:
        rule_def = SARIF_RULES.get(
            f.rule_id,
            {
                "id": f.rule_id,
                "name": "EbpfFinding",
                "shortDescription": {"text": f.message},
                "defaultConfiguration": {"level": "warning"},
            },
        )
        results.append(
            {
                "ruleId": f.rule_id,
                "level": rule_def["defaultConfiguration"]["level"],
                "message": {"text": f.message},
                "locations": [
                    {
                        "physicalLocation": {
                            "artifactLocation": {"uri": target_uri},
                            "region": {"startLine": 1},
                        }
                    }
                ],
                "properties": {
                    "process": f.process,
                    "pid": f.pid,
                    "detail": f.detail,
                    "recommendation": f.recommendation,
                    "findingHash": f.finding_hash,
                },
            }
        )

    return {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "vibes-ebpf-tracer",
                        "informationUri": "https://github.com/dan-petty/vibes",
                        "version": TRACER_VERSION,
                        "rules": list(SARIF_RULES.values()),
                    }
                },
                "results": results,
            }
        ],
    }


def export_json(summary: EbpfAuditSummary) -> str:
    """Export audit summary and findings as JSON string."""
    data = {
        "tracer_version": TRACER_VERSION,
        "total_events": summary.total_events,
        "total_violations": summary.total_violations,
        "is_compliant": summary.is_compliant,
        "findings_by_rule": summary.findings_by_rule,
        "findings": [
            {
                "rule_id": f.rule_id,
                "severity": f.severity.value,
                "message": f.message,
                "process": f.process,
                "pid": f.pid,
                "detail": f.detail,
                "recommendation": f.recommendation,
            }
            for f in summary.findings
        ],
    }
    return json.dumps(data, indent=2)


def format_markdown_report(summary: EbpfAuditSummary) -> str:
    """Format eBPF audit summary as a GitHub Flavored Markdown report."""
    lines = [
        "# eBPF Agent Sandbox Introspection Report",
        "",
        "| Metric | Value |",
        "| :--- | :--- |",
        f"| **Tracer Version** | `{TRACER_VERSION}` |",
        f"| **Total Events Audited** | {summary.total_events} |",
        f"| **Total Invariant Violations** | {summary.total_violations} |",
        f"| **Sandbox Compliance Status** | {'✓ COMPLIANT' if summary.is_compliant else '❌ NON-COMPLIANT'} |",
        "",
    ]
    if not summary.findings:
        lines.append(
            "✓ **Zero runtime kernel invariant violations detected.** Agent sandbox verified secure.\n"
        )
        return "\n".join(lines)

    lines.extend(
        [
            "## Runtime Kernel Invariant Findings",
            "",
            "| Rule | Severity | Process (PID) | Finding | Recommendation |",
            "| :--- | :--- | :--- | :--- | :--- |",
        ]
    )
    for f in summary.findings:
        lines.append(
            f"| `{f.rule_id}` | **{f.severity.value.upper()}** | `{f.process} ({f.pid})` | {f.message} | {f.recommendation} |"
        )
    lines.append("")
    return "\n".join(lines)


def _handle_cli_exports(
    summary: EbpfAuditSummary,
    events: Sequence[EbpfTraceEvent],
    target_uri: str,
    args: argparse.Namespace,
) -> None:
    """Write outputs to the file paths that the command line requests."""
    sarif_p = args.export_sarif
    if sarif_p:
        data = export_sarif(summary, target_uri)
        Path(sarif_p).write_text(json.dumps(data, indent=2), encoding="utf-8")

    json_p = args.export_json
    if json_p:
        Path(json_p).write_text(export_json(summary), encoding="utf-8")

    md_p = args.export_md
    if md_p:
        Path(md_p).write_text(format_markdown_report(summary), encoding="utf-8")

    tetragon_p = args.export_tetragon
    if tetragon_p:
        policy = generate_tetragon_policy(events, pod_label=args.pod_label)
        Path(tetragon_p).write_text(json.dumps(policy, indent=2), encoding="utf-8")

    falco_p = args.export_falco
    if falco_p:
        Path(falco_p).write_text(generate_falco_policy(events), encoding="utf-8")


def _absolute_dir(text: str) -> str:
    """Accept an absolute directory path: kernel events carry absolute paths only."""
    if not text.startswith("/"):
        raise argparse.ArgumentTypeError(f"not an absolute path: {text}")
    return text


def _pod_label(text: str) -> tuple[str, str]:
    """Split a `key=value` pod label at the first `=`; the key must not be empty."""
    key, sep, value = text.partition("=")
    if not sep or not key:
        raise argparse.ArgumentTypeError(f"not a key=value label: {text}")
    return key, value


def _build_argument_parser() -> argparse.ArgumentParser:
    """Construct command-line argument parser."""
    parser = argparse.ArgumentParser(
        prog="ebpf_tracer.py",
        description="eBPF Process Tracing & Agent Sandbox Introspection Oracle.",
    )
    parser.add_argument(
        "targets",
        nargs="*",
        default=[],
        help="JSONL files of flat event records or native Tetragon process_exec records.",
    )
    parser.add_argument(
        "--home-dir",
        action="append",
        type=_absolute_dir,
        metavar="PATH",
        help="Home directory of the sandbox user; the ~/ sensitive paths are checked below it. "
        "Repeat for more users (default: /root).",
    )
    parser.add_argument(
        "--pod-label",
        type=_pod_label,
        default=TETRAGON_POD_LABEL,
        metavar="KEY=VALUE",
        help="Label of the sandbox pods that the Tetragon policy's podSelector matches "
        "(default: app.kubernetes.io/name=agent-sandbox).",
    )
    parser.add_argument(
        "--preset",
        choices=[p.value for p in TracerPreset],
        default=TracerPreset.STRICT.value,
        help="Audit sensitivity preset (default: strict).",
    )
    parser.add_argument(
        "--export-sarif",
        metavar="PATH",
        help="Export findings as SARIF 2.1.0 to PATH.",
    )
    parser.add_argument(
        "--export-json",
        metavar="PATH",
        help="Export audit summary as JSON to PATH.",
    )
    parser.add_argument(
        "--export-md",
        metavar="PATH",
        help="Export Markdown report to PATH.",
    )
    parser.add_argument(
        "--export-tetragon",
        metavar="PATH",
        help="Export synthesized Tetragon TracingPolicy to PATH.",
    )
    parser.add_argument(
        "--export-falco",
        metavar="PATH",
        help="Export synthesized Falco rules to PATH.",
    )
    return parser


def _load_events_from_targets(targets: Sequence[str]) -> list[EbpfTraceEvent]:
    """Load trace events from specified file paths or standard input."""
    if not targets:
        text = sys.stdin.read()
        return parse_events_jsonl(text) if text.strip() else []

    events: list[EbpfTraceEvent] = []
    for t in targets:
        p = Path(t)
        if p.is_file():
            events.extend(parse_events_jsonl(p.read_text(encoding="utf-8")))
    return events


def main(argv: Sequence[str] | None = None) -> int:
    """Execute main CLI routine."""
    parser = _build_argument_parser()
    args = parser.parse_args(argv)

    events = _load_events_from_targets(args.targets)

    preset = TracerPreset(args.preset)
    tracer = EbpfSandboxTracer(preset, args.home_dir or DEFAULT_HOME_DIRS)
    summary = tracer.audit_events(events)

    _handle_cli_exports(
        summary,
        events,
        args.targets[0] if args.targets else "trace.jsonl",
        args,
    )
    print(format_markdown_report(summary))
    return 0 if summary.is_compliant else 1


if __name__ == "__main__":
    sys.exit(main())
