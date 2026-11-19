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
from typing import Any, Final

import pytest

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

# One `process_exec` record in the JSON that Tetragon writes (`tetra getevents -o json` and
# the export file): field names from api/v1/tetragon/events.proto and tetragon.proto, v1.8.0.
TETRAGON_PROCESS_EXEC: Final[dict[str, Any]] = {
    "process_exec": {
        "process": {
            "exec_id": "ZXhhbXBsZS1ub2RlOjQyNDI=",
            "pid": 4242,
            "uid": 1000,
            "cwd": "/workspace",
            "binary": "/usr/bin/nc",
            "arguments": "-lvp 4444",
            "flags": "execve clone",
            "start_time": "2026-11-19T10:00:00.123456789Z",
            "auid": 4294967295,
            "pod": {"namespace": "default", "name": "agent-sandbox"},
            "parent_exec_id": "ZXhhbXBsZS1ub2RlOjQyMDA=",
            "tid": 4242,
        },
        "parent": {"pid": 4200, "binary": "/bin/bash", "arguments": "-c ./run.sh"},
    },
    "node_name": "<worker-node>",
    "time": "2026-11-19T10:00:00.123456789Z",
}

# How Tetragon applies the string operators of the `sys_execve` selectors.
TETRAGON_STRING_OPS: Final[dict[str, Any]] = {"Equal": str.__eq__, "Postfix": str.endswith}


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


def test_parse_native_tetragon_process_exec() -> None:
    """Verify that a native Tetragon `process_exec` record becomes one execve event that the audit reads."""
    flat_line = '{"event_type": "file_open", "pid": 7, "comm": "cat", "path": "/etc/shadow"}'
    events = parse_events_jsonl(json.dumps(TETRAGON_PROCESS_EXEC) + "\n" + flat_line + "\n")
    summary = EbpfSandboxTracer(TracerPreset.STRICT).audit_events(events)

    assert (
        parse_trace_event(TETRAGON_PROCESS_EXEC),
        [e.event_type for e in events],
        [(f.rule_id, f.pid) for f in summary.findings],
    ) == (
        EbpfTraceEvent(
            event_type=EbpfEventType.EXECVE,
            pid=4242,
            process_name="nc",
            command_line="/usr/bin/nc -lvp 4444",
            target_path="/usr/bin/nc",
            syscall="execve",
            timestamp=1795082400.123456,
        ),
        [EbpfEventType.EXECVE, EbpfEventType.FILE_OPEN],
        [("EBPF001", 4242), ("EBPF003", 7)],
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


def _file_open_rules(tracer: EbpfSandboxTracer, paths: list[str]) -> dict[str, list[str]]:
    """Audit a file open of each path and map the path to the rule IDs of its findings."""
    return {
        p: [f.rule_id for f in tracer.audit_event(EbpfTraceEvent(EbpfEventType.FILE_OPEN, 1, "cat", target_path=p))]
        for p in paths
    }


def test_ebpf003_expands_home_and_matches_whole_components() -> None:
    """Verify that `~/` entries match below each configured home directory, by whole path components."""
    paths = [
        "/home/agent/.ssh/id_ed25519",
        "/home/agent/.sshkeys/id_ed25519",
        "/home/agent/.config/gcloud/credentials.db",
        "/opt/.ssh/config",
        "/root/.ssh/config",
        "/rootfs/etc/hosts",
        "/etc/shadowed",
        "/etc/./shadow",
        "src/.ssh/config",
    ]

    assert (
        _file_open_rules(EbpfSandboxTracer(TracerPreset.STRICT, home_dirs=("/home/agent",)), paths),
        _file_open_rules(EbpfSandboxTracer(TracerPreset.STRICT), ["/root/.ssh/config", "/home/agent/.ssh/id_ed25519"]),
    ) == (
        {
            "/home/agent/.ssh/id_ed25519": ["EBPF003"],
            "/home/agent/.sshkeys/id_ed25519": [],
            "/home/agent/.config/gcloud/credentials.db": ["EBPF003"],
            "/opt/.ssh/config": [],
            "/root/.ssh/config": ["EBPF003"],
            "/rootfs/etc/hosts": [],
            "/etc/shadowed": [],
            "/etc/./shadow": ["EBPF003"],
            "src/.ssh/config": [],
        },
        {"/root/.ssh/config": ["EBPF003"], "/home/agent/.ssh/id_ed25519": []},
    )


def test_ebpf004_capset_is_a_syscall_and_cap_set_proc_is_not() -> None:
    """Verify that the kernel syscall `capset` fires EBPF004 and the libcap function `cap_set_proc` does not."""
    tracer = EbpfSandboxTracer(TracerPreset.STRICT)
    rules = {
        name: [f.rule_id for f in tracer.audit_event(EbpfTraceEvent(EbpfEventType.UNKNOWN, 1, "x", syscall=name))]
        for name in ("capset", "cap_set_proc")
    }
    assert rules == {"capset": ["EBPF004"], "cap_set_proc": []}


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
        tetragon_crd["metadata"],
        "rule: custom_agent_rule" in falco_rules,
    ) == (
        "TracingPolicy",
        {"name": "custom-agent-policy", "annotations": {"ebpf-tracer/observed-binaries": "git,python3"}},
        True,
    )


def test_tetragon_policy_uses_only_tracingpolicy_crd_fields() -> None:
    """Verify the syscall kprobe and that each key is a field of the Tetragon TracingPolicy CRD.

    Field names and enums follow the CRD of Tetragon v1.8.0
    (pkg/k8s/apis/cilium.io/client/crds/v1alpha1/cilium.io_tracingpolicies.yaml). The CRD
    has no `allowedProcesses` field; `kubectl apply` rejects an unknown field. The pod
    selector limits the Sigkill to the sandbox pods of resources/k8s/agent-sandbox/.
    """
    policy = generate_tetragon_policy([EbpfTraceEvent(EbpfEventType.EXECVE, 1, "python3")])
    custom = generate_tetragon_policy([], pod_label=("team", "agents"))
    kprobe = policy["spec"]["kprobes"][0]
    binaries = ["chattr", "nc", "ncat", "netcat", "nmap", "socat", "tcpdump", "telnet", "wireshark"]

    assert (
        sorted(policy),
        sorted(policy["spec"]),
        policy["spec"]["podSelector"],
        custom["spec"]["podSelector"],
        len(policy["spec"]["kprobes"]),
        (kprobe["call"], kprobe["syscall"], kprobe["args"]),
        kprobe["selectors"],
    ) == (
        ["apiVersion", "kind", "metadata", "spec"],
        ["kprobes", "podSelector"],
        {"matchLabels": {"app.kubernetes.io/name": "agent-sandbox"}},
        {"matchLabels": {"team": "agents"}},
        1,
        ("sys_execve", True, [{"index": 0, "type": "string"}]),
        [
            {
                "matchArgs": [{"index": 0, "operator": "Equal", "values": binaries}],
                "matchActions": [{"action": "Sigkill"}],
            },
            {
                "matchArgs": [{"index": 0, "operator": "Postfix", "values": [f"/{b}" for b in binaries]}],
                "matchActions": [{"action": "Sigkill"}],
            },
        ],
    )


def _kernel_kills(kprobe: dict[str, Any], path: str) -> bool:
    """Apply the selectors of a kprobe to a path the way Tetragon does: OR over selectors, AND over matches."""
    return any(
        all(any(TETRAGON_STRING_OPS[m["operator"]](path, v) for v in m["values"]) for m in sel["matchArgs"])
        for sel in kprobe["selectors"]
    )


def test_tetragon_selectors_kill_where_the_audit_flags() -> None:
    """Verify that the `sys_execve` selectors kill the paths that EBPF001 flags, and no other path."""
    kprobe = generate_tetragon_policy([])["spec"]["kprobes"][0]
    tracer = EbpfSandboxTracer(TracerPreset.STRICT)
    paths = ["/usr/bin/nc", "/bin/ncat", "nc", "./socat", "/usr/bin/ncal", "/usr/bin/nc.openbsd", "/usr/bin/python3"]

    assert {p: _kernel_kills(kprobe, p) for p in paths} == {
        p: bool(tracer.audit_event(EbpfTraceEvent(EbpfEventType.EXECVE, 1, p))) for p in paths
    }


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

    exit_code = main([
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
    ])

    assert (
        exit_code,
        sarif_file.is_file(),
        json_file.is_file(),
        md_file.is_file(),
        tetragon_file.is_file(),
        falco_file.is_file(),
    ) == (1, True, True, True, True, True)


def test_cli_home_dir_option(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Verify that `--home-dir` sets the home directories of the `~/` entries and needs an absolute path."""
    trace_file = tmp_path / "agent_events.jsonl"
    trace_file.write_text(
        '{"event_type": "file_open", "pid": 1, "comm": "cat", "path": "/home/agent/.aws/credentials"}\n',
        encoding="utf-8",
    )
    default_code = main([str(trace_file)])
    agent_code = main([str(trace_file), "--home-dir", "/srv", "--home-dir", "/home/agent"])
    with pytest.raises(SystemExit) as relative:
        main([str(trace_file), "--home-dir", "home/agent"])

    assert (default_code, agent_code, relative.value.code, "not an absolute path" in capsys.readouterr().err) == (
        0,
        1,
        2,
        True,
    )


def test_cli_pod_label_option(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Verify that `--pod-label` sets the pod selector of the exported policy and needs `key=value`."""
    trace_file = tmp_path / "agent_events.jsonl"
    trace_file.write_text('{"event_type": "execve", "pid": 1, "comm": "python3"}\n', encoding="utf-8")
    default_file, custom_file = tmp_path / "default.json", tmp_path / "custom.json"
    main([str(trace_file), "--export-tetragon", str(default_file)])
    main([str(trace_file), "--export-tetragon", str(custom_file), "--pod-label", "team=agents"])
    with pytest.raises(SystemExit) as no_value:
        main([str(trace_file), "--pod-label", "team"])

    assert (
        json.loads(default_file.read_text(encoding="utf-8"))["spec"]["podSelector"],
        json.loads(custom_file.read_text(encoding="utf-8"))["spec"]["podSelector"],
        no_value.value.code,
        "not a key=value label" in capsys.readouterr().err,
    ) == (
        {"matchLabels": {"app.kubernetes.io/name": "agent-sandbox"}},
        {"matchLabels": {"team": "agents"}},
        2,
        True,
    )

