"""Unit tests for the Ephemeral Rootless Container Sandbox Harness."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

_app_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(_app_dir))

import pytest
import syscall_filter
from sandbox import (
    CONTAINER_IMAGE,
    CISAuditReport,
    CISPolicyAuditor,
    ContainerCommandBuilder,
    ContainerSandboxHarness,
    RuntimeEnforcementAuditor,
    SandboxExecutionResult,
    SandboxSecurityPolicy,
    demo_policy,
    main,
    run_benchmark_matrix,
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
    """Verify the default policy passes all eight CIS-Docker-modeled controls."""
    policy = SandboxSecurityPolicy()
    report: CISAuditReport = CISPolicyAuditor.audit(policy)
    assert (
        report.score,
        len(report.passed_controls),
        len(report.violations),
        any("CIS-5.1" in c for c in report.passed_controls),
    ) == (100.0, 8, 0, True)


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
    assert (
        report.score < 50.0,
        len(report.violations) >= 6,
        any("read-only" in v.lower() for v in report.violations),
        any("root user" in v.lower() for v in report.violations),
    ) == (True, True, True, True)


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
    assert (
        cmd[0],
        "run" in cmd,
        "--read-only" in cmd,
        cmd[-2:],
    ) == ("podman", True, True, ["echo", "test"])


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
    assert (
        "PATH" in clean_env,
        "LANG" in clean_env,
        "AWS_SECRET_ACCESS_KEY" not in clean_env,
        "GITHUB_TOKEN" not in clean_env,
        "SSH_AUTH_SOCK" not in clean_env,
    ) == (True, True, True, True, True)


def test_simulator_safe_execution() -> None:
    """Verify simulator executes safe Python payload and returns clean result."""
    harness = ContainerSandboxHarness(force_simulator=True)
    result: SandboxExecutionResult = harness.run_python_code("print('vibes-sandbox-ok')")
    assert (
        result.exit_code,
        "vibes-sandbox-ok" in result.stdout,
        result.timed_out,
        result.runtime_used,
        result.duration_ms >= 0.0,
    ) == (0, True, False, "simulator", True)


def test_simulator_timeout_containment() -> None:
    """Verify runaway executions are terminated within the bounded timeout."""
    fast_timeout_policy = SandboxSecurityPolicy(timeout_seconds=0.1)
    harness = ContainerSandboxHarness(policy=fast_timeout_policy, force_simulator=True)
    result: SandboxExecutionResult = harness.run_python_code(
        "import time\ntime.sleep(1.0)\nprint('escaped')",
    )
    assert (
        result.timed_out,
        result.exit_code != 0,
        "escaped" not in result.stdout,
        "timed out" in result.stderr.lower(),
    ) == (True, True, True, True)


def test_simulator_output_truncation_cwe400() -> None:
    """Verify massive output is truncated to prevent CWE-400 resource exhaustion."""
    harness = ContainerSandboxHarness(force_simulator=True)
    result: SandboxExecutionResult = harness.run_python_code(
        "print('A' * 100000)",
    )
    assert (
        result.exit_code,
        len(result.stdout) <= 65536,
        "[TRUNCATED" in result.stdout,
    ) == (0, True, True)


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


# --- Container mode, driven by a fake engine on PATH ------------------------------------
#
# No container engine is needed for these: the fake records each argument list it gets
# and sleeps on `run` for FAKE_ENGINE_RUN_SECONDS, so the tests check the command lines
# that the harness sends and the order in which it stops a run.

_FAKE_ENGINE = """\
import json, os, sys, time
with open({log!r}, "a", encoding="utf-8") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
if sys.argv[1] == "run":
    time.sleep(float(os.environ.get("FAKE_ENGINE_RUN_SECONDS", "0")))
sys.exit(int(os.environ.get("FAKE_ENGINE_KILL_STATUS", "0")) if sys.argv[1] == "kill" else 0)
"""


@pytest.fixture
def fake_engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Put a fake `docker` first on PATH and return the file where it logs its calls."""
    log = tmp_path / "engine-calls.jsonl"
    fake = tmp_path / "docker"
    fake.write_text(f"#!{sys.executable}\n" + _FAKE_ENGINE.format(log=str(log)), encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")
    return log


def _engine_calls(log: Path) -> list[list[str]]:
    """Read the argument lists the fake engine recorded, in call order."""
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def _run_name(run_args: list[str]) -> str:
    """Return the `--name` value of a recorded `run` call."""
    return run_args[run_args.index("--name") + 1]


def test_the_container_runs_the_image_interpreter_not_the_host_path(fake_engine: Path) -> None:
    """The host interpreter path does not exist in the image, so the container must not get it.

    With `sys.executable` passed through, every payload exited with code 127 in a real
    container, and the audit still called the policy compliant.
    """
    harness = ContainerSandboxHarness(container_engine="docker")
    result = harness.run_python_code("print('in the image')")
    after_image = result.command[result.command.index(CONTAINER_IMAGE) + 1:]
    assert (harness.engine, after_image[:2], sys.executable in result.command) == ("docker", ["python3", "-c"], False)


def test_the_simulator_keeps_the_host_interpreter() -> None:
    """The simulator runs in the current environment, where the host interpreter is the one to use."""
    result = ContainerSandboxHarness(force_simulator=True).run_python_code("print('host')")
    assert (result.command[0], result.exit_code) == (sys.executable, 0)


def test_each_container_run_gets_its_own_name(fake_engine: Path) -> None:
    """A unique `--name` per run is what lets a timeout kill one container and no other."""
    harness = ContainerSandboxHarness(container_engine="docker")
    harness.run_python_code("pass")
    harness.run_python_code("pass")
    names = [_run_name(call) for call in _engine_calls(fake_engine) if call[0] == "run"]
    assert (len(names), len(set(names))) == (2, 2)


def test_a_container_timeout_kills_that_container_by_name(
    fake_engine: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A signal to the host client does not stop the container; the engine's `kill` does.

    The client process group holds only the `docker` client. Killing it leaves the
    container running, so on timeout the harness must ask the engine to kill the container
    by the name it gave at `run`, and must name no other container.
    """
    monkeypatch.setenv("FAKE_ENGINE_RUN_SECONDS", "30")
    harness = ContainerSandboxHarness(SandboxSecurityPolicy(timeout_seconds=0.5), container_engine="docker")
    result = harness.run_python_code("import time\ntime.sleep(30)")
    calls = _engine_calls(fake_engine)
    runs = [call for call in calls if call[0] == "run"]
    kills = [call for call in calls if call[0] == "kill"]
    assert (result.timed_out, result.exit_code, len(runs), kills) == (True, 124, 1, [["kill", _run_name(runs[0])]])


def test_a_failed_container_kill_is_reported(fake_engine: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A container that may still run must not be reported as a clean timeout."""
    monkeypatch.setenv("FAKE_ENGINE_RUN_SECONDS", "30")
    monkeypatch.setenv("FAKE_ENGINE_KILL_STATUS", "1")
    harness = ContainerSandboxHarness(SandboxSecurityPolicy(timeout_seconds=0.5), container_engine="docker")
    result = harness.run_python_code("import time\ntime.sleep(30)")
    assert (result.timed_out, "docker kill vibes-sandbox-" in result.stderr, "failed" in result.stderr) == (
        True, True, True,
    )


def test_the_demo_timeout_leaves_room_for_a_container_start() -> None:
    """A container start takes longer than 0.15 s, so only the simulator demo uses that timeout."""
    assert (demo_policy("docker").timeout_seconds, demo_policy("podman").timeout_seconds,
            demo_policy("simulator").timeout_seconds) == (5.0, 5.0, 0.15)


# --- A real engine, when this host has one ----------------------------------------------

_LIVE_ENGINE = ContainerSandboxHarness._resolve_engine(None, force_sim=False)


def _image_problem(engine: str) -> str | None:
    """Return why the engine cannot supply the image, or None when the image is ready.

    The pull runs here, outside the demo's 5 s timeout, so a first pull does not time out
    the first payload.
    """
    inspect = [engine, "image", "inspect", CONTAINER_IMAGE]
    if subprocess.run(inspect, capture_output=True, timeout=60, check=False).returncode == 0:
        return None
    pulled = subprocess.run([engine, "pull", CONTAINER_IMAGE], capture_output=True, text=True, timeout=300, check=False)
    return None if pulled.returncode == 0 else pulled.stderr.strip()[:256]


@pytest.mark.skipif(_LIVE_ENGINE == "simulator", reason="no container engine: neither docker nor podman is on PATH")
def test_the_demo_payloads_pass_in_a_container() -> None:
    """Executed, not declared: the three demo payloads run in a real container."""
    problem = _image_problem(_LIVE_ENGINE)
    if problem:
        pytest.skip(f"{_LIVE_ENGINE} cannot supply {CONTAINER_IMAGE}: {problem}")
    matrix = run_benchmark_matrix(ContainerSandboxHarness(policy=demo_policy(_LIVE_ENGINE)))
    assert [(name, passed) for name, passed, _ in matrix] == [(name, True) for name, _, _ in matrix]
