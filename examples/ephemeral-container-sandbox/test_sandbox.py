"""Unit tests for the Ephemeral Rootless Container Sandbox Harness."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_app_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(_app_dir))

from sandbox import (
    CISAuditReport,
    CISPolicyAuditor,
    ContainerCommandBuilder,
    ContainerSandboxHarness,
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
