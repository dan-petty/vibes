"""Unit and integration test suite for eBPF Process Tracing & Sandbox Introspection.

Validates kernel telemetry parsing, unauthorized binary execution (EBPF001), network egress
violations (EBPF002), sensitive file tampering (EBPF003), privilege escalation (EBPF004),
anti-debugging evasion (EBPF005), Tetragon/Falco policy synthesis, and SARIF exports.
All assertions use structural tuple equality checks to maintain proactive complexity headroom.
"""

from __future__ import annotations

import json
import re
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

# How Tetragon applies the string operators of the exec kprobe selectors.
TETRAGON_STRING_OPS: Final[dict[str, Any]] = {"Equal": str.__eq__, "Postfix": str.endswith}

# The path that each exec hook reads for one `execveat(fd, "", argv, envp, AT_EMPTY_PATH)`
# call, which glibc `fexecve` makes, of the Debian 13 `nc`. The system call gets the empty
# path. `security_bprm_check` gets the file that the kernel opened: `/usr/bin/nc` links to
# the alternatives target `nc.openbsd` of the netcat-openbsd package.
FEXECVE_NC_HOOK_PATHS: Final[dict[str, list[str]]] = {
    "sys_execveat": [""],
    "security_bprm_check": ["/usr/bin/nc.openbsd"],
}

# The names that the exec kprobes kill: the deny list and the files that its links resolve to.
DENIED_EXEC_NAMES: Final[list[str]] = [
    "chattr",
    "inetutils-telnet",
    "nc",
    "nc.openbsd",
    "nc.traditional",
    "ncat",
    "netcat",
    "nmap",
    "socat",
    "socat1",
    "tcpdump",
    "telnet",
    "telnet.netkit",
    "wireshark",
]


def _tetragon_kprobe(binary: str, arguments: str, function_name: str, args: list[dict[str, Any]]) -> dict[str, Any]:
    """Return a native Tetragon `process_kprobe` record of one call in the sandbox pod.

    Field names from api/v1/tetragon/tetragon.proto (ProcessKprobe, KprobeArgument), v1.8.0.
    A syscall kprobe names the kernel symbol, such as `__x64_sys_setuid`.
    """
    return {
        "process_kprobe": {
            "process": {
                "exec_id": "ZXhhbXBsZS1ub2RlOjQzMDA=",
                "pid": 4300,
                "uid": 1000,
                "cwd": "/workspace",
                "binary": binary,
                "arguments": arguments,
                "flags": "execve clone",
                "start_time": "2026-11-19T10:00:00.123456789Z",
                "pod": {"namespace": "default", "name": "agent-sandbox"},
                "tid": 4300,
            },
            "parent": {"pid": 4200, "binary": "/bin/bash", "arguments": "-c ./run.sh"},
            "function_name": function_name,
            "args": args,
            "action": "KPROBE_ACTION_POST",
            "policy_name": "agent-sandbox-containment",
        },
        "node_name": "<worker-node>",
        "time": "2026-11-19T10:00:01.5Z",
    }


def _falco_alert(rule: str, output_fields: dict[str, Any]) -> dict[str, Any]:
    """Return a Falco JSON alert (`json_output: true`); `output_fields` holds the fields of the rule output."""
    return {
        "hostname": "<worker-node>",
        "output": f"10:00:01.500000000: Warning {rule}",
        "priority": "Warning",
        "rule": rule,
        "source": "syscall",
        "tags": ["agent", "ebpf", "sandbox"],
        "time": "2026-11-19T10:00:01.500000000Z",
        "output_fields": {"evt.time": 1795082401500000000, "container.image.repository": None, **output_fields},
    }


# Each native record and the flat record that describes the same call; both must give the same finding.
NATIVE_AND_FLAT_RECORDS: Final[dict[str, tuple[dict[str, Any], dict[str, Any]]]] = {
    "kprobe-execveat-EBPF001": (
        _tetragon_kprobe("/bin/sh", "-c nc -lvp 4444", "__x64_sys_execveat", [{"string_arg": "/usr/bin/nc"}]),
        {"event_type": "execve", "syscall": "execveat", "pid": 4300, "comm": "nc", "args": "/usr/bin/nc"},
    ),
    # `fexecve` of the Debian 13 `nc`: the LSM hook reads the file that `/usr/bin/nc` links to.
    "kprobe-bprm_check-EBPF001": (
        _tetragon_kprobe(
            "/usr/bin/python3",
            "run.py",
            "security_bprm_check",
            [{"linux_binprm_arg": {"path": "/usr/bin/nc.openbsd", "permission": "-rwxr-xr-x"}}],
        ),
        {
            "event_type": "execve",
            "syscall": "security_bprm_check",
            "pid": 4300,
            "comm": "nc.openbsd",
            "args": "/usr/bin/nc.openbsd",
        },
    ),
    "kprobe-tcp_connect-EBPF002": (
        _tetragon_kprobe(
            "/usr/bin/curl",
            "http://169.254.169.254/latest/meta-data/",
            "tcp_connect",
            [
                {
                    "sock_arg": {
                        "family": "AF_INET",
                        "type": "SOCK_STREAM",
                        "protocol": "IPPROTO_TCP",
                        "saddr": "192.0.2.10",
                        "daddr": "169.254.169.254",
                        "sport": 40112,
                        "dport": 80,
                        "state": "TCP_SYN_SENT",
                    }
                }
            ],
        ),
        {"event_type": "connect", "syscall": "tcp_connect", "pid": 4300, "comm": "curl", "daddr": "169.254.169.254", "dport": 80},
    ),
    "kprobe-file_permission-EBPF003": (
        _tetragon_kprobe(
            "/bin/cat",
            "/etc/passwd",
            "security_file_permission",
            [{"file_arg": {"path": "/etc/passwd", "permission": "-rw-r--r--"}}, {"int_arg": 4}],
        ),
        {"event_type": "file_open", "pid": 4300, "comm": "cat", "path": "/etc/passwd"},
    ),
    "kprobe-setuid-EBPF004": (
        _tetragon_kprobe("/bin/su", "root", "__x64_sys_setuid", [{"int_arg": 0}]),
        {"event_type": "privilege_change", "syscall": "setuid", "pid": 4300, "comm": "su"},
    ),
    "kprobe-ptrace-EBPF005": (
        _tetragon_kprobe("/usr/bin/gdb", "-p 1", "__x64_sys_ptrace", [{"long_arg": 16}]),
        {"event_type": "ptrace_evasion", "syscall": "ptrace", "pid": 4300, "comm": "gdb", "args": "/usr/bin/gdb -p 1"},
    ),
    "falco-execve-EBPF001": (
        _falco_alert(
            "agent_sandbox_least_privilege",
            {"evt.type": "execve", "proc.name": "nc", "proc.pid": 4300, "proc.cmdline": "nc -lvp 4444", "fd.name": None},
        ),
        {"event_type": "execve", "syscall": "execve", "pid": 4300, "comm": "nc", "args": "nc -lvp 4444"},
    ),
    "falco-connect-EBPF002": (
        _falco_alert(
            "Outbound connection to metadata",
            {
                "evt.type": "connect",
                "fd.name": "192.0.2.10:40112->169.254.169.254:80",
                "fd.rip": "169.254.169.254",
                "fd.rport": 80,
                "proc.name": "curl",
                "proc.cmdline": "curl http://169.254.169.254/latest/meta-data/",
            },
        ),
        {"event_type": "connect", "syscall": "connect", "comm": "curl", "daddr": "169.254.169.254", "dport": 80},
    ),
    "falco-openat-EBPF003": (
        _falco_alert(
            "Read sensitive file untrusted",
            {"evt.type": "openat", "fd.name": "/etc/shadow", "proc.name": "cat", "proc.cmdline": "cat /etc/shadow"},
        ),
        {"event_type": "openat", "comm": "cat", "path": "/etc/shadow"},
    ),
    "falco-setuid-EBPF004": (
        _falco_alert("Non sudo setuid", {"evt.type": "setuid", "proc.name": "su", "proc.cmdline": "su root"}),
        {"event_type": "privilege_change", "syscall": "setuid", "comm": "su"},
    ),
    "falco-ptrace-EBPF005": (
        _falco_alert("PTRACE attached to process", {"evt.type": "ptrace", "proc.name": "gdb", "proc.cmdline": "gdb -p 1"}),
        {"syscall": "ptrace", "comm": "gdb", "args": "gdb -p 1"},
    ),
}


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


@pytest.mark.parametrize("case", sorted(NATIVE_AND_FLAT_RECORDS))
def test_native_kprobe_and_falco_records_give_the_flat_record_finding(case: str) -> None:
    """Verify that a native Tetragon `process_kprobe` record or a Falco alert gives the finding of its flat record."""
    native, flat = NATIVE_AND_FLAT_RECORDS[case]
    tracer = EbpfSandboxTracer(TracerPreset.STRICT)
    native_findings = tracer.audit_event(parse_trace_event(native))

    assert ([f.rule_id for f in native_findings], native_findings) == (
        [case.rsplit("-", 1)[1]],
        tracer.audit_event(parse_trace_event(flat)),
    )


def test_parse_native_exec_kprobe_and_falco_alerts() -> None:
    """Verify the events of an exec kprobe record and of Falco alerts with and without `evt.type`.

    An exec hook fires before the new program runs, so `process` is still the caller and the
    path argument names the program. The Falco alert without `evt.type` is the example of the
    Falco output documentation; its event type stays unknown.
    """
    execveat = NATIVE_AND_FLAT_RECORDS["kprobe-execveat-EBPF001"][0]
    falco_exec = NATIVE_AND_FLAT_RECORDS["falco-execve-EBPF001"][0]
    shell_alert = {
        "hostname": "falco-xczjd",
        "priority": "Critical",
        "rule": "Terminal shell in container",
        "source": "syscall",
        "time": "2023-05-25T13:44:05.478445995Z",
        "output_fields": {
            "container.id": "ee97d9c4186f",
            "evt.time": 1685022245478445995,
            "k8s.ns.name": "default",
            "proc.cmdline": "sh -c clear; (bash || ash || sh)",
            "proc.name": "sh",
            "proc.pname": "runc",
            "proc.tty": 34816,
            "user.loginuid": -1,
            "user.name": "root",
        },
    }

    assert [parse_trace_event(r) for r in (execveat, falco_exec, shell_alert)] == [
        EbpfTraceEvent(
            event_type=EbpfEventType.EXECVE,
            pid=4300,
            process_name="nc",
            command_line="/usr/bin/nc",
            target_path="/usr/bin/nc",
            syscall="execveat",
            timestamp=1795082401.5,
        ),
        EbpfTraceEvent(
            event_type=EbpfEventType.EXECVE,
            pid=4300,
            process_name="nc",
            command_line="nc -lvp 4444",
            syscall="execve",
            timestamp=1795082401.5,
        ),
        EbpfTraceEvent(
            event_type=EbpfEventType.UNKNOWN,
            pid=0,
            process_name="sh",
            command_line="sh -c clear; (bash || ash || sh)",
            timestamp=1685022245.478445,
        ),
    ]


def test_falco_rule_output_names_the_fields_that_the_parser_reads() -> None:
    """Verify that an alert of the generated Falco rule carries the event type and the PID."""
    assert sorted(re.findall(r"%([\w.]+)", generate_falco_policy([]))) == [
        "evt.type",
        "proc.cmdline",
        "proc.name",
        "proc.pid",
    ]


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
    """Verify the exec kprobes and that each key is a field of the TracingPolicy CRD.

    The `execve` and `execveat` syscall kprobes match the path that the caller gives. The
    `security_bprm_check` kprobe matches the file that the kernel opened (type
    `linux_binprm`), which is an absolute path, so it needs no `Equal` selector.
    Field names and enums follow the CRD of Tetragon v1.8.0
    (pkg/k8s/apis/cilium.io/client/crds/v1alpha1/cilium.io_tracingpolicies.yaml). The CRD
    has no `allowedProcesses` field; `kubectl apply` rejects an unknown field. The pod
    selector limits the Sigkill to the sandbox pods of resources/k8s/agent-sandbox/.
    """
    policy = generate_tetragon_policy([EbpfTraceEvent(EbpfEventType.EXECVE, 1, "python3")])
    custom = generate_tetragon_policy([], pod_label=("team", "agents"))
    binaries = DENIED_EXEC_NAMES
    file_kprobe = {
        "call": "security_bprm_check",
        "syscall": False,
        "args": [{"index": 0, "type": "linux_binprm"}],
        "selectors": [
            {
                "matchArgs": [{"index": 0, "operator": "Postfix", "values": [f"/{b}" for b in binaries]}],
                "matchActions": [{"action": "Sigkill"}],
            }
        ],
    }

    assert (
        sorted(policy),
        sorted(policy["spec"]),
        policy["spec"]["podSelector"],
        custom["spec"]["podSelector"],
        policy["spec"]["kprobes"],
    ) == (
        ["apiVersion", "kind", "metadata", "spec"],
        ["kprobes", "podSelector"],
        {"matchLabels": {"app.kubernetes.io/name": "agent-sandbox"}},
        {"matchLabels": {"team": "agents"}},
        [
            {
                "call": call,
                "syscall": True,
                "args": [{"index": index, "type": "string"}],
                "selectors": [
                    {
                        "matchArgs": [{"index": index, "operator": "Equal", "values": binaries}],
                        "matchActions": [{"action": "Sigkill"}],
                    },
                    {
                        "matchArgs": [{"index": index, "operator": "Postfix", "values": [f"/{b}" for b in binaries]}],
                        "matchActions": [{"action": "Sigkill"}],
                    },
                ],
            }
            # execve(path, argv, envp) and execveat(dirfd, path, argv, envp, flags): the path index differs.
            for call, index in (("sys_execve", 0), ("sys_execveat", 1))
        ]
        + [file_kprobe],
    )


def _kernel_kills(kprobe: dict[str, Any], path: str) -> bool:
    """Apply the selectors of a kprobe to a path the way Tetragon does: OR over selectors, AND over matches."""
    return any(
        all(any(TETRAGON_STRING_OPS[m["operator"]](path, v) for v in m["values"]) for m in sel["matchArgs"])
        for sel in kprobe["selectors"]
    )


def _kills_by_hook(kprobes: list[dict[str, Any]], paths: dict[str, list[str]]) -> dict[str, dict[str, bool]]:
    """Apply each kprobe whose call is a key of `paths` to the paths that its hook reads."""
    hooked = [k for k in kprobes if k["call"] in paths]
    return {k["call"]: {p: _kernel_kills(k, p) for p in paths[k["call"]]} for k in hooked}


def test_tetragon_selectors_kill_where_the_audit_flags() -> None:
    """Verify that the selectors of each exec kprobe kill the paths that EBPF001 flags, and no other path.

    The LSM hook reads an absolute path, so it gets no bare or relative name. A busybox
    applet, such as the Alpine `nc`, resolves to `busybox`: only the syscall kprobes see its name.
    """
    kprobes = generate_tetragon_policy([])["spec"]["kprobes"]
    tracer = EbpfSandboxTracer(TracerPreset.STRICT)
    files = ["/usr/bin/nc", "/bin/ncat", "/usr/bin/ncal", "/usr/bin/ncfoo", "/usr/bin/nc.openbsd"]
    files += ["/usr/bin/socat1", "/usr/bin/busybox", "/usr/bin/python3"]
    names = [*files, "nc", "./socat", "nc.traditional"]
    paths = {"sys_execve": names, "sys_execveat": names, "security_bprm_check": files}
    flagged = {p: bool(tracer.audit_event(EbpfTraceEvent(EbpfEventType.EXECVE, 1, p))) for p in names}

    assert _kills_by_hook(kprobes, paths) == {
        call: {p: flagged[p] for p in hook_paths} for call, hook_paths in paths.items()
    }


def test_tetragon_policy_kills_a_denied_binary_run_through_fexecve() -> None:
    """Verify that the policy kills `nc` run with `AT_EMPTY_PATH`, which gives the syscall an empty path."""
    kprobes = generate_tetragon_policy([])["spec"]["kprobes"]

    assert _kills_by_hook(kprobes, FEXECVE_NC_HOOK_PATHS) == {
        "sys_execveat": {"": False},
        "security_bprm_check": {"/usr/bin/nc.openbsd": True},
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

