"""Unit and integration test suite for eBPF Process Tracing & Sandbox Introspection.

Validates kernel telemetry parsing, unauthorized binary execution (EBPF001), network egress
violations (EBPF002), sensitive file tampering (EBPF003), privilege escalation (EBPF004),
anti-debugging evasion (EBPF005), Tetragon/Falco policy synthesis, and SARIF exports.
All assertions use structural tuple equality checks to maintain proactive complexity headroom.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Final

REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

from ebpf_tracer import (
    EbpfAuditSummary,
    EbpfEventType,
    EbpfFinding,
    EbpfSandboxTracer,
    EbpfSeverity,
    EbpfTraceEvent,
    TracerPreset,
    export_json,
    export_sarif,
    format_markdown_report,
    generate_falco_policy,
    generate_tetragon_policy,
    main,
    parse_events_jsonl,
    parse_trace_event,
)


def test_parse_trace_event_normalization() -> None:
    """Verify raw event dictionary normalization into typed EbpfTraceEvent."""
    raw = {
        "event_type": "process_exec",
        "pid": 4201,
        "comm": "pytest",
        "args": "pytest tests/",
        "syscall": "execve",
        "timestamp": 1720000000.5,
    }
    event = parse_trace_event(raw)
    assert (
        event.event_type,
        event.pid,
        event.process_name,
        event.syscall,
    ) == (
        EbpfEventType.EXECVE,
        4201,
        "pytest",
        "execve",
    )


def test_parse_events_jsonl_stream() -> None:
    """Verify parsing multiline JSONL eBPF telemetry stream."""
    stream = (
        '{"event_type": "execve", "pid": 100, "process_name": "python", "command_line": "python main.py"}\n'
        "# Comment line should be ignored\n"
        '{"event_type": "file_open", "pid": 100, "process_name": "python", "target_path": "src/app.py"}\n'
    )
    events = parse_events_jsonl(stream)
    types = [e.event_type for e in events]
    assert (len(events), types) == (
        2,
        [EbpfEventType.EXECVE, EbpfEventType.FILE_OPEN],
    )


def test_clean_trace_has_zero_violations() -> None:
    """Verify benign compiler, test runner, and file operations pass cleanly."""
    events = [
        EbpfTraceEvent(
            event_type=EbpfEventType.EXECVE,
            pid=1001,
            process_name="python3",
            command_line="python3 -m unittest",
            syscall="execve",
        ),
        EbpfTraceEvent(
            event_type=EbpfEventType.FILE_OPEN,
            pid=1001,
            process_name="python3",
            target_path="src/service.py",
            syscall="openat",
        ),
    ]
    tracer = EbpfSandboxTracer(TracerPreset.STRICT)
    summary = tracer.audit_events(events)
    assert (summary.total_violations, summary.is_compliant, len(summary.findings)) == (0, True, 0)


def test_ebpf001_unauthorized_binary_execution() -> None:
    """Verify disallowed utilities and interactive shells trigger EBPF001."""
    events = [
        EbpfTraceEvent(
            event_type=EbpfEventType.EXECVE,
            pid=2001,
            process_name="nc",
            command_line="nc -lvp 4444",
            syscall="execve",
        ),
        EbpfTraceEvent(
            event_type=EbpfEventType.EXECVE,
            pid=2002,
            process_name="bash",
            command_line="bash -i",
            syscall="execve",
        ),
    ]
    tracer = EbpfSandboxTracer(TracerPreset.STRICT)
    summary = tracer.audit_events(events)
    rule_ids = [f.rule_id for f in summary.findings]
    severities = [f.severity for f in summary.findings]
    assert (len(summary.findings), rule_ids, severities) == (
        2,
        ["EBPF001", "EBPF001"],
        [EbpfSeverity.CRITICAL, EbpfSeverity.CRITICAL],
    )


def test_ebpf002_network_egress_violation() -> None:
    """Verify socket connection to cloud metadata endpoint triggers EBPF002."""
    events = [
        EbpfTraceEvent(
            event_type=EbpfEventType.SOCKET_CONNECT,
            pid=3001,
            process_name="curl",
            command_line="curl http://169.254.169.254/latest/meta-data/",
            destination_ip="169.254.169.254",
            destination_port=80,
            syscall="connect",
        )
    ]
    tracer = EbpfSandboxTracer(TracerPreset.STRICT)
    summary = tracer.audit_events(events)
    rule_ids = [f.rule_id for f in summary.findings]
    severities = [f.severity for f in summary.findings]
    assert (len(summary.findings), rule_ids, severities) == (
        1,
        ["EBPF002"],
        [EbpfSeverity.HIGH],
    )


def test_ebpf003_sensitive_file_tampering() -> None:
    """Verify attempted access to /etc/shadow or credentials triggers EBPF003."""
    events = [
        EbpfTraceEvent(
            event_type=EbpfEventType.FILE_OPEN,
            pid=4001,
            process_name="cat",
            command_line="cat /etc/shadow",
            target_path="/etc/shadow",
            syscall="openat",
        )
    ]
    tracer = EbpfSandboxTracer(TracerPreset.STRICT)
    summary = tracer.audit_events(events)
    rule_ids = [f.rule_id for f in summary.findings]
    severities = [f.severity for f in summary.findings]
    assert (len(summary.findings), rule_ids, severities) == (
        1,
        ["EBPF003"],
        [EbpfSeverity.CRITICAL],
    )


def test_ebpf004_privilege_escalation_syscall() -> None:
    """Verify unauthorized privilege change or unshare triggers EBPF004."""
    events = [
        EbpfTraceEvent(
            event_type=EbpfEventType.PRIVILEGE_CHANGE,
            pid=5001,
            process_name="escalate",
            command_line="./escalate",
            syscall="setuid",
        )
    ]
    tracer = EbpfSandboxTracer(TracerPreset.STRICT)
    summary = tracer.audit_events(events)
    rule_ids = [f.rule_id for f in summary.findings]
    severities = [f.severity for f in summary.findings]
    assert (len(summary.findings), rule_ids, severities) == (
        1,
        ["EBPF004"],
        [EbpfSeverity.HIGH],
    )


def test_ebpf005_anti_debugging_evasion() -> None:
    """Verify ptrace anti-debugging invocation triggers EBPF005."""
    events = [
        EbpfTraceEvent(
            event_type=EbpfEventType.PTRACE_EVASION,
            pid=6001,
            process_name="evade",
            command_line="./evade",
            syscall="ptrace",
        )
    ]
    tracer = EbpfSandboxTracer(TracerPreset.STRICT)
    summary = tracer.audit_events(events)
    rule_ids = [f.rule_id for f in summary.findings]
    severities = [f.severity for f in summary.findings]
    assert (len(summary.findings), rule_ids, severities) == (
        1,
        ["EBPF005"],
        [EbpfSeverity.HIGH],
    )


def test_preset_network_only_filtering() -> None:
    """Verify network_only preset evaluates only network events."""
    events = [
        EbpfTraceEvent(
            event_type=EbpfEventType.EXECVE,
            pid=7001,
            process_name="nc",
            command_line="nc -l 8080",
            syscall="execve",
        ),
        EbpfTraceEvent(
            event_type=EbpfEventType.SOCKET_CONNECT,
            pid=7002,
            process_name="curl",
            command_line="curl http://169.254.169.254",
            destination_ip="169.254.169.254",
            destination_port=80,
            syscall="connect",
        ),
    ]
    net_tracer = EbpfSandboxTracer(TracerPreset.NETWORK_ONLY)
    summary = net_tracer.audit_events(events)
    rule_ids = [f.rule_id for f in summary.findings]
    assert (len(summary.findings), rule_ids) == (1, ["EBPF002"])


def test_policy_synthesis_tetragon_and_falco() -> None:
    """Verify automated synthesis of Tetragon TracingPolicy and Falco rules."""
    events = [
        EbpfTraceEvent(
            event_type=EbpfEventType.EXECVE,
            pid=8001,
            process_name="python3",
            command_line="python3 app.py",
        ),
        EbpfTraceEvent(
            event_type=EbpfEventType.EXECVE,
            pid=8002,
            process_name="git",
            command_line="git status",
        ),
    ]
    tetragon_crd = generate_tetragon_policy(events, "custom-agent-policy")
    falco_rules = generate_falco_policy(events, "custom_agent_rule")

    assert (
        tetragon_crd["kind"],
        tetragon_crd["metadata"]["name"],
        "python3" in tetragon_crd["spec"]["allowedProcesses"],
        "git" in tetragon_crd["spec"]["allowedProcesses"],
        "rule: custom_agent_rule" in falco_rules,
    ) == (
        "TracingPolicy",
        "custom-agent-policy",
        True,
        True,
        True,
    )


def test_sarif_export_structure() -> None:
    """Verify SARIF 2.1.0 telemetry export schema adherence."""
    finding = EbpfFinding(
        rule_id="EBPF001",
        severity=EbpfSeverity.CRITICAL,
        message="Unauthorized binary nc",
        process="nc",
        pid=9001,
        detail="Command: nc -l 8080",
        recommendation="Pin container allowed binaries",
        finding_hash="hash9001",
    )
    summary = EbpfAuditSummary(
        total_events=5,
        total_violations=1,
        is_compliant=False,
        findings=(finding,),
        findings_by_rule={"EBPF001": 1},
    )
    sarif = export_sarif(summary, "test_trace.jsonl")
    driver = sarif["runs"][0]["tool"]["driver"]
    results = sarif["runs"][0]["results"]
    assert (
        sarif["version"],
        driver["name"],
        len(results),
        results[0]["ruleId"],
        results[0]["level"],
    ) == (
        "2.1.0",
        "vibes-ebpf-tracer",
        1,
        "EBPF001",
        "error",
    )


def test_json_and_markdown_formatting() -> None:
    """Verify JSON and Markdown report formatting."""
    summary = EbpfAuditSummary(
        total_events=10,
        total_violations=0,
        is_compliant=True,
        findings=(),
        findings_by_rule={},
    )
    json_out = export_json(summary)
    data = json.loads(json_out)
    md_out = format_markdown_report(summary)
    assert (
        data["total_events"],
        data["is_compliant"],
        "Zero runtime kernel invariant violations detected" in md_out,
    ) == (10, True, True)


def test_cli_end_to_end_with_temp_files(tmp_path: Path) -> None:
    """Verify CLI audit end-to-end with temporary JSONL trace files and exports."""
    trace_file = tmp_path / "agent_events.jsonl"
    trace_file.write_text(
        '{"event_type": "execve", "pid": 1111, "process_name": "nc", "command_line": "nc -lvp 9999"}\n',
        encoding="utf-8",
    )
    sarif_file = tmp_path / "out.sarif"
    json_file = tmp_path / "out.json"
    md_file = tmp_path / "out.md"
    tetragon_file = tmp_path / "policy.json"
    falco_file = tmp_path / "rules.yaml"

    exit_code = main(
        [
            str(trace_file),
            "--preset",
            "strict",
            "--export-sarif",
            str(sarif_file),
            "--export-json",
            str(json_file),
            "--export-md",
            str(md_file),
            "--export-tetragon",
            str(tetragon_file),
            "--export-falco",
            str(falco_file),
        ]
    )

    assert (
        exit_code,
        sarif_file.is_file(),
        json_file.is_file(),
        md_file.is_file(),
        tetragon_file.is_file(),
        falco_file.is_file(),
    ) == (1, True, True, True, True, True)
