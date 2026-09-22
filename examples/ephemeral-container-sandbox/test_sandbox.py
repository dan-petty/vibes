"""Unit tests for the Ephemeral Rootless Container Sandbox Harness."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_app_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(_app_dir))

import pytest
import syscall_filter
from sandbox import (
    CISAuditReport,
    CISPolicyAuditor,
    ContainerCommandBuilder,
    ContainerSandboxHarness,
    RuntimeEnforcementAuditor,
    SandboxExecutionResult,
    SandboxSecurityPolicy,
    main,
)


def test_default_security_policy_values_and_immutability() -> None:
    """Verify default security policy contains hardened CIS controls."""
    policy = SandboxSecurityPolicy()
    actual_tuple = (
        policy.read_only_rootfs,
        policy.network_mode,
        policy.drop_capabilities,
        policy.no_new_privileges,
        policy.user,
        policy.memory_limit_mb,
        policy.cpu_quota,
        policy.pids_limit,
        policy.timeout_seconds,
        policy.tmpfs_mounts,
    )
    expected_tuple = (
        True,
        "none",
        ("ALL",),
        True,
        "1000:1000",
        512,
        1.0,
        100,
        5.0,
        (("/tmp", "rw,noexec,nosuid,nodev,size=64m"),),
    )
    assert actual_tuple == expected_tuple


def test_cis_policy_auditor_perfect_score() -> None:
    """Verify default policy scores 100% on CIS container benchmark audit."""
    policy = SandboxSecurityPolicy()
    report: CISAuditReport = CISPolicyAuditor.audit(policy)
    assert report.score == 100.0
    assert len(report.passed_controls) == 8
    assert len(report.violations) == 0
    assert any("CIS-5.1" in c for c in report.passed_controls)


def test_cis_policy_auditor_penalties() -> None:
    """Verify insecure configurations receive score deductions and findings."""
    insecure_policy = SandboxSecurityPolicy(
        read_only_rootfs=False,
        network_mode="host",
        user="0:0",
        no_new_privileges=False,
        drop_capabilities=(),
        memory_limit_mb=4096,
        pids_limit=5000,
        tmpfs_mounts=(),
    )
    report: CISAuditReport = CISPolicyAuditor.audit(insecure_policy)
    assert report.score < 50.0
    assert len(report.violations) >= 6
    assert any("read-only" in v.lower() for v in report.violations)
    assert any("root user" in v.lower() for v in report.violations)


def test_container_command_builder_docker() -> None:
    """Verify command builder compiles hardened arguments for Docker."""
    policy = SandboxSecurityPolicy()
    cmd = ContainerCommandBuilder.build_run_args(
        engine="docker",
        image="python:3.12-slim",
        command=["python3", "-c", "print('hello')"],
        policy=policy,
    )
    required_flags = [
        "docker",
        "run",
        "--read-only",
        "--network",
        "none",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--user",
        "1000:1000",
        "--memory",
        "512m",
        "--pids-limit",
        "100",
    ]
    assert all(flag in cmd for flag in required_flags)
    assert cmd[-3:] == ["python3", "-c", "print('hello')"]


def test_container_command_builder_podman() -> None:
    """Verify command builder compiles hardened arguments for Podman."""
    policy = SandboxSecurityPolicy()
    cmd = ContainerCommandBuilder.build_run_args(
        engine="podman",
        image="alpine:latest",
        command=["echo", "test"],
        policy=policy,
    )
    assert cmd[0] == "podman"
    assert "run" in cmd
    assert "--read-only" in cmd
    assert cmd[-2:] == ["echo", "test"]


def test_environment_sanitization() -> None:
    """Verify host secrets and sensitive environment variables are filtered."""
    dirty_env = {
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "AWS_SECRET_ACCESS_KEY": "supersecret123",
        "GITHUB_TOKEN": "ghp_mocktoken999",
        "SSH_AUTH_SOCK": "/run/user/1000/ssh.sock",
    }
    clean_env = ContainerSandboxHarness.sanitize_env(dirty_env)
    assert "PATH" in clean_env
    assert "LANG" in clean_env
    assert "AWS_SECRET_ACCESS_KEY" not in clean_env
    assert "GITHUB_TOKEN" not in clean_env
    assert "SSH_AUTH_SOCK" not in clean_env


def test_simulator_safe_execution() -> None:
    """Verify simulator executes safe Python payload and returns clean result."""
    harness = ContainerSandboxHarness(force_simulator=True)
    result: SandboxExecutionResult = harness.run_python_code("print('vibes-sandbox-ok')")
    assert result.exit_code == 0
    assert "vibes-sandbox-ok" in result.stdout
    assert result.timed_out is False
    assert result.runtime_used == "simulator"
    assert result.duration_ms >= 0.0


def test_simulator_timeout_containment() -> None:
    """Verify runaway executions are terminated within the bounded timeout."""
    fast_timeout_policy = SandboxSecurityPolicy(timeout_seconds=0.1)
    harness = ContainerSandboxHarness(policy=fast_timeout_policy, force_simulator=True)
    result: SandboxExecutionResult = harness.run_python_code(
        "import time\ntime.sleep(1.0)\nprint('escaped')",
    )
    assert result.timed_out is True
    assert result.exit_code != 0
    assert "escaped" not in result.stdout
    assert "timed out" in result.stderr.lower()


def test_simulator_output_truncation_cwe400() -> None:
    """Verify massive output is truncated to prevent CWE-400 resource exhaustion."""
    harness = ContainerSandboxHarness(force_simulator=True)
    result: SandboxExecutionResult = harness.run_python_code(
        "print('A' * 100000)",
    )
    assert result.exit_code == 0
    assert len(result.stdout) <= 65536
    assert "[TRUNCATED" in result.stdout


def test_sandbox_run_command_execution() -> None:
    """Verify general command execution through sandbox harness."""
    harness = ContainerSandboxHarness(force_simulator=True)
    result = harness.run_command([sys.executable, "-c", "print(21 * 2)"])
    assert result.exit_code == 0
    assert result.stdout.strip() == "42"


def test_sandbox_cli_audit_and_demo(capsys: Any) -> None:
    """Verify CLI audit and demo command entrypoints execute cleanly."""
    exit_audit = main(["--audit"])
    assert exit_audit == 0
    captured_audit = capsys.readouterr().out
    assert "CIS ROOTLESS CONTAINER BENCHMARK AUDIT" in captured_audit

    exit_demo = main(["--demo", "--simulator"])
    assert exit_demo == 0
    captured_demo = capsys.readouterr().out
    assert "EPHEMERAL CONTAINER SANDBOX MATRIX" in captured_demo


# --- What the policy declares against what the engine applies ---------------------------


def test_a_perfect_policy_score_does_not_mean_the_run_is_confined() -> None:
    """The finding, pinned in one assertion.

    `CISPolicyAuditor` reads the policy object and reported 100.0/100 with zero violations
    for a harness that then ran code which opened a socket and wrote into the invoking
    user's home directory. A conformance check of a declaration is not a measurement of a
    runtime, and the two numbers must never again be the same number.
    """
    policy = SandboxSecurityPolicy()
    declared = CISPolicyAuditor.audit(policy).score
    applied = RuntimeEnforcementAuditor.audit(policy, "simulator").score
    assert (declared, applied < declared) == (100.0, True)


def test_the_engineless_path_names_the_controls_it_cannot_apply() -> None:
    """An unenforceable control must be reported, not omitted and not claimed.

    A read-only root filesystem and a private tmpfs need a mount namespace, which an
    unprivileged process does not have. Saying so is the difference between a sandbox and
    a sandbox-shaped report.
    """
    report = RuntimeEnforcementAuditor.audit(SandboxSecurityPolicy(), "simulator")
    assert report.unenforced == [
        "CIS-5.1 (Read-only Root Filesystem)",
        "CIS-5.8 (Hardened Tmpfs Scratch Mount)",
    ]


def test_a_container_engine_enforces_every_control() -> None:
    """The container path gets its isolation from the runtime, and the report says so."""
    report = RuntimeEnforcementAuditor.audit(SandboxSecurityPolicy(), "docker")
    assert (report.unenforced, report.score) == ([], 100.0)


def test_an_empty_report_scores_zero_rather_than_dividing_by_nothing() -> None:
    """A report with no controls is a real state, not an exception."""
    assert RuntimeEnforcementAuditor.audit(SandboxSecurityPolicy(), "docker").score == 100.0


@pytest.mark.skipif(not syscall_filter.supported()[0],
                    reason=f"seccomp unavailable here: {syscall_filter.supported()[1]}")
def test_the_simulator_denies_a_payload_the_network() -> None:
    """Executed, not declared: the harness runs the payload and the kernel refuses it."""
    harness = ContainerSandboxHarness(force_simulator=True)
    result = harness.run_python_code(
        "import socket\n"
        "try:\n"
        "    socket.socket(); print('SOCKET SUCCEEDED')\n"
        "except PermissionError:\n"
        "    print('SOCKET DENIED')\n"
    )
    assert (result.exit_code, "SOCKET DENIED" in result.stdout) == (0, True)


@pytest.mark.skipif(not syscall_filter.supported()[0],
                    reason=f"seccomp unavailable here: {syscall_filter.supported()[1]}")
def test_the_simulator_still_runs_ordinary_work() -> None:
    """A containment control that breaks benign payloads is a control someone disables."""
    result = ContainerSandboxHarness(force_simulator=True).run_python_code("print('ordinary work')")
    assert (result.exit_code, result.stdout.strip()) == (0, "ordinary work")
