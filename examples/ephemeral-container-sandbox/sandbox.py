#!/usr/bin/env python3
"""Ephemeral Rootless Container Sandbox Harness.

Provides isolated, ephemeral execution of untrusted agent payloads inside
hardened rootless containers (Docker/Podman) with standard-library simulator
fallback.

Key Hardening Features:
- CIS Rootless Container Security Benchmark compliance auditing.
- Deny-all egress network isolation (--network none).
- Cgroups v2 resource quotas (memory caps, cpu bounds, pids limits).
- Read-only root filesystem with ephemeral tmpfs scratch mounts.
- Dropped Linux capabilities (CAP_DROP ALL) and no-new-privileges flag.
- Stripped, zero-trust sanitized environment variables (no secret leakage).
- Output stream length capping to prevent denial of service (CWE-400).
"""

from __future__ import annotations

import argparse
import math
import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

# Kept importable whether this file is run from its own directory, imported by a test
# runner rooted elsewhere, or copied out of the repository, which is what an exhibit is for.
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

# Imported after the path is arranged above; E402 is not in this project's ruff selection, and a
# `noqa` for an unselected rule is itself a finding (RUF100).
import syscall_filter

# Bounded output constraints
MAX_OUTPUT_BYTES = 65536
TRUNCATION_MARKER = "\n... [TRUNCATED DUE TO BUFFER LIMIT] ...\n"
DEFAULT_SAFE_VARS = frozenset({"PATH", "LANG", "LC_ALL", "PYTHONUNBUFFERED", "PYTHONDONTWRITEBYTECODE"})

# The eight controls, in the order both auditors report them.
CIS_CONTROL_NAMES: tuple[str, ...] = (
    "CIS-5.1 (Read-only Root Filesystem)",
    "CIS-5.2 (Network Isolation Egress Deny-All)",
    "CIS-5.3 (Capabilities Dropped)",
    "CIS-5.4 (No New Privileges)",
    "CIS-5.5 (Unprivileged User)",
    "CIS-5.6 (Cgroups Memory Cap)",
    "CIS-5.7 (PID Limit Fork-Bomb Protection)",
    "CIS-5.8 (Hardened Tmpfs Scratch Mount)",
)


@dataclass(frozen=True)
class SandboxSecurityPolicy:
    """Immutable security policy defining containment boundaries."""

    read_only_rootfs: bool = True
    network_mode: str = "none"
    drop_capabilities: tuple[str, ...] = ("ALL",)
    no_new_privileges: bool = True
    user: str = "1000:1000"
    memory_limit_mb: int = 512
    cpu_quota: float = 1.0
    pids_limit: int = 100
    timeout_seconds: float = 5.0
    tmpfs_mounts: tuple[tuple[str, str], ...] = (("/tmp", "rw,noexec,nosuid,nodev,size=64m"),)
    allowed_env_vars: tuple[str, ...] = ("PATH", "LANG", "LC_ALL", "PYTHONUNBUFFERED")
    # Which syscall groups the engine-less path denies in the kernel. The container
    # path gets its isolation from the runtime; this is what the other path has.
    seccomp_groups: tuple[str, ...] = syscall_filter.DEFAULT_GROUPS


@dataclass
class SandboxExecutionResult:
    """Execution telemetry captured from a sandboxed run."""

    command: list[str]
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: float
    timed_out: bool = False
    memory_exceeded: bool = False
    policy_violations: list[str] = field(default_factory=list)
    runtime_used: str = "simulator"


@dataclass
class CISAuditReport:
    """Audit scorecard measuring compliance with CIS container benchmarks."""

    score: float
    passed_controls: list[str]
    violations: list[str]
    recommendations: list[str]


class CISPolicyAuditor:
    """Audits sandbox policies against CIS Docker/Rootless Security Benchmarks."""

    @classmethod
    def audit(cls, policy: SandboxSecurityPolicy) -> CISAuditReport:
        """Audit policy against 8 canonical CIS rootless container controls."""
        passed: list[str] = []
        violations: list[str] = []
        recs: list[str] = []

        cls._check_read_only(policy, passed, violations, recs)
        cls._check_network(policy, passed, violations, recs)
        cls._check_capabilities(policy, passed, violations, recs)
        cls._check_privileges(policy, passed, violations, recs)
        cls._check_user(policy, passed, violations, recs)
        cls._check_memory(policy, passed, violations, recs)
        cls._check_pids(policy, passed, violations, recs)
        cls._check_tmpfs(policy, passed, violations, recs)

        total_controls = 8
        score = round((len(passed) / total_controls) * 100.0, 1)
        return CISAuditReport(score=score, passed_controls=passed, violations=violations, recommendations=recs)

    @staticmethod
    def _check_read_only(p: SandboxSecurityPolicy, passed: list[str], v: list[str], r: list[str]) -> None:
        if p.read_only_rootfs:
            passed.append("CIS-5.1 (Read-only Root Filesystem)")
        else:
            v.append("CIS-5.1: Root filesystem is mutable (read-only rootfs disabled), allowing persistent tampering.")
            r.append("Enable read_only_rootfs=True to prevent persistent compromise.")

    @staticmethod
    def _check_network(p: SandboxSecurityPolicy, passed: list[str], v: list[str], r: list[str]) -> None:
        if p.network_mode == "none":
            passed.append("CIS-5.2 (Network Isolation Egress Deny-All)")
        else:
            v.append(f"CIS-5.2: Insecure network mode '{p.network_mode}'. Egress allowed.")
            r.append("Set network_mode='none' to enforce strict egress isolation.")

    @staticmethod
    def _check_capabilities(p: SandboxSecurityPolicy, passed: list[str], v: list[str], r: list[str]) -> None:
        if "ALL" in p.drop_capabilities:
            passed.append("CIS-5.3 (Capabilities Dropped)")
        else:
            v.append("CIS-5.3: Linux capabilities not dropped (missing CAP_DROP ALL).")
            r.append("Set drop_capabilities=('ALL',) to strip kernel privileges.")

    @staticmethod
    def _check_privileges(p: SandboxSecurityPolicy, passed: list[str], v: list[str], r: list[str]) -> None:
        if p.no_new_privileges:
            passed.append("CIS-5.4 (No New Privileges)")
        else:
            v.append("CIS-5.4: Process can acquire new privileges via setuid binaries.")
            r.append("Set no_new_privileges=True to block privilege escalation.")

    @staticmethod
    def _check_user(p: SandboxSecurityPolicy, passed: list[str], v: list[str], r: list[str]) -> None:
        if p.user and p.user not in ("0", "0:0", "root"):
            passed.append("CIS-5.5 (Unprivileged User)")
        else:
            v.append("CIS-5.5: Container runs as root user (UID 0).")
            r.append("Set user='1000:1000' or dedicated non-root UID.")

    @staticmethod
    def _check_memory(p: SandboxSecurityPolicy, passed: list[str], v: list[str], r: list[str]) -> None:
        if 0 < p.memory_limit_mb <= 1024:
            passed.append("CIS-5.6 (Cgroups Memory Cap)")
        else:
            v.append(f"CIS-5.6: Excessive or unconstrained memory limit ({p.memory_limit_mb}MB).")
            r.append("Bound memory_limit_mb <= 1024MB to prevent host memory exhaustion.")

    @staticmethod
    def _check_pids(p: SandboxSecurityPolicy, passed: list[str], v: list[str], r: list[str]) -> None:
        if 0 < p.pids_limit <= 256:
            passed.append("CIS-5.7 (PID Limit Fork-Bomb Protection)")
        else:
            v.append(f"CIS-5.7: High or unbounded PID limit ({p.pids_limit}).")
            r.append("Set pids_limit <= 256 to mitigate fork-bomb exploits.")

    @staticmethod
    def _check_tmpfs(p: SandboxSecurityPolicy, passed: list[str], v: list[str], r: list[str]) -> None:
        has_safe_tmp = any(dst == "/tmp" and "noexec" in opts for dst, opts in p.tmpfs_mounts)
        if has_safe_tmp:
            passed.append("CIS-5.8 (Hardened Tmpfs Scratch Mount)")
        else:
            v.append("CIS-5.8: Ephemeral scratch /tmp missing hardened noexec tmpfs mount.")
            r.append("Mount /tmp with rw,noexec,nosuid,nodev,size=64m.")


@dataclass(frozen=True)
class ControlEnforcement:
    """One control, and whether the engine about to run actually applies it."""

    control: str
    enforced: bool
    mechanism: str


@dataclass
class EnforcementReport:
    """What the runtime enforces, as distinct from what the policy declares."""

    engine: str
    controls: list[ControlEnforcement] = field(default_factory=list)

    @property
    def score(self) -> float:
        """Return the share of controls this engine actually applies."""
        if not self.controls:
            return 0.0
        return round(100.0 * sum(c.enforced for c in self.controls) / len(self.controls), 1)

    @property
    def unenforced(self) -> list[str]:
        """Return the controls the policy declares and this engine does not apply."""
        return [c.control for c in self.controls if not c.enforced]


class RuntimeEnforcementAuditor:
    """Audit what the engine enforces, not what the policy asks for.

    `CISPolicyAuditor` reads the policy object. That is a useful check of the declaration
    and it was the only check there was, so a default policy scored 100.0/100 with zero
    violations while the engine-less path enforced none of the eight controls — it ran code
    that opened a socket and wrote into the invoking user's home directory, and reported a
    perfect score for the run. A conformance check of a declaration is not a measurement of
    a runtime, and presenting one as the other is the failure mode of
    [Observation 11](../../observations/systems/11-silent-certification-failure-and-gate-integrity.md).
    """

    @classmethod
    def audit(cls, policy: SandboxSecurityPolicy, engine: str) -> EnforcementReport:
        """Report per-control enforcement for the engine that will actually run."""
        if engine != "simulator":
            return EnforcementReport(engine, [
                ControlEnforcement(name, True, f"{engine} run flag")
                for name in CIS_CONTROL_NAMES
            ])
        return EnforcementReport(engine, cls._simulator_controls(policy))

    @classmethod
    def _simulator_controls(cls, policy: SandboxSecurityPolicy) -> list[ControlEnforcement]:
        """Describe what an unprivileged process can and cannot enforce on itself."""
        ok, reason = syscall_filter.supported()
        seccomp = f"seccomp-BPF ({', '.join(policy.seccomp_groups)})" if ok else f"unavailable: {reason}"
        denied = set(policy.seccomp_groups) if ok else set()
        return [
            ControlEnforcement(CIS_CONTROL_NAMES[0], False,
                               "no mount namespace; an unprivileged process cannot remount its root"),
            ControlEnforcement(CIS_CONTROL_NAMES[1], "network" in denied, seccomp),
            ControlEnforcement(CIS_CONTROL_NAMES[2], "privilege" in denied, seccomp),
            ControlEnforcement(CIS_CONTROL_NAMES[3], ok, "prctl(PR_SET_NO_NEW_PRIVS)"),
            ControlEnforcement(CIS_CONTROL_NAMES[4], os.getuid() != 0,
                               f"the harness runs as uid {os.getuid()} and cannot drop to another"),
            ControlEnforcement(CIS_CONTROL_NAMES[5], True, f"RLIMIT_AS at {policy.memory_limit_mb}MB"),
            ControlEnforcement(CIS_CONTROL_NAMES[6], True,
                               f"RLIMIT_NPROC at {policy.pids_limit}, per-uid rather than per-tree"),
            ControlEnforcement(CIS_CONTROL_NAMES[7], False,
                               "no mount namespace; /tmp is the host's"),
        ]


class ContainerCommandBuilder:
    """Compiles hardened container CLI execution argument lists."""

    @classmethod
    def build_run_args(
        cls,
        engine: str,
        image: str,
        command: list[str],
        policy: SandboxSecurityPolicy,
    ) -> list[str]:
        """Construct secure argument list without shell interpolation."""
        args: list[str] = [engine, "run", "--rm"]

        if policy.read_only_rootfs:
            args.append("--read-only")

        args.extend(["--network", policy.network_mode])

        for cap in policy.drop_capabilities:
            args.extend(["--cap-drop", cap])

        if policy.no_new_privileges:
            args.extend(["--security-opt", "no-new-privileges:true"])

        if policy.user:
            args.extend(["--user", policy.user])

        args.extend(["--memory", f"{policy.memory_limit_mb}m"])
        args.extend(["--cpus", str(policy.cpu_quota)])
        args.extend(["--pids-limit", str(policy.pids_limit)])

        for target_dir, mount_opts in policy.tmpfs_mounts:
            args.extend(["--tmpfs", f"{target_dir}:{mount_opts}"])

        args.append(image)
        args.extend(command)
        return args


class ContainerSandboxHarness:
    """Orchestrates hardened sandbox executions with simulator fallback."""

    def __init__(
        self,
        policy: SandboxSecurityPolicy | None = None,
        force_simulator: bool = False,
        container_engine: str | None = None,
    ) -> None:
        """Initialize sandbox harness with specified policy and engine."""
        self.policy = policy or SandboxSecurityPolicy()
        self.force_simulator = force_simulator
        self.engine = self._resolve_engine(container_engine, force_simulator)

    @staticmethod
    def _resolve_engine(requested: str | None, force_sim: bool) -> str:
        if force_sim:
            return "simulator"
        if requested in ("docker", "podman") and shutil.which(requested):
            return requested
        if shutil.which("docker"):
            return "docker"
        if shutil.which("podman"):
            return "podman"
        return "simulator"

    @classmethod
    def sanitize_env(cls, env: dict[str, str]) -> dict[str, str]:
        """Filter environment variables to whitelisted subset."""
        return {k: v for k, v in env.items() if k in DEFAULT_SAFE_VARS}

    def run_command(self, command: list[str]) -> SandboxExecutionResult:
        """Execute command inside container or simulator sandbox."""
        if self.engine == "simulator":
            return self._run_simulator(command)
        return self._run_container(command)

    def run_python_code(self, python_code: str) -> SandboxExecutionResult:
        """Execute a Python code string inside the sandbox."""
        cmd = [sys.executable, "-c", python_code]
        return self.run_command(cmd)

    def _run_container(self, command: list[str]) -> SandboxExecutionResult:
        docker_cmd = [sys.executable if c == sys.executable else c for c in command]
        image = "python:3.12-slim"
        run_args = ContainerCommandBuilder.build_run_args(
            engine=self.engine,
            image=image,
            command=docker_cmd,
            policy=self.policy,
        )
        return self._execute_subprocess(run_args, runtime_name=self.engine)

    def _run_simulator(self, command: list[str]) -> SandboxExecutionResult:
        sanitized_env = self.sanitize_env(dict(os.environ))
        return self._execute_subprocess(
            command, runtime_name="simulator", env=sanitized_env, confine=self._confinement()
        )

    def _confinement(self) -> Callable[[], None] | None:
        """Build the child-side confinement, or None when this host cannot apply it.

        Built in the parent so the child does nothing between `fork` and `exec` but call
        two already-resolved `prctl`s and four `setrlimit`s. Returning None rather than
        raising is deliberate: an unenforceable control is reported as unenforced by
        `RuntimeEnforcementAuditor`, and a sandbox that quietly runs unconfined is the
        defect this was added to fix.
        """
        supported, _ = syscall_filter.supported()
        if not supported:
            return None
        install = syscall_filter.installer(self.policy.seccomp_groups)
        limit = syscall_filter.limiter(
            memory_mb=self.policy.memory_limit_mb,
            cpu_seconds=max(1, math.ceil(self.policy.timeout_seconds)),
            max_processes=self.policy.pids_limit,
        )

        def confine() -> None:
            """Cap resources first, then filter; the filter cannot be undone."""
            limit()
            install()

        return confine

    def _execute_subprocess(
        self,
        cmd: list[str],
        runtime_name: str,
        env: dict[str, str] | None = None,
        confine: Callable[[], None] | None = None,
    ) -> SandboxExecutionResult:
        start = time.monotonic()
        timed_out = False
        exit_code = 0
        stdout_raw = ""
        stderr_raw = ""

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
                start_new_session=True,
                preexec_fn=confine,
            )
            stdout_raw, stderr_raw = proc.communicate(timeout=self.policy.timeout_seconds)
            exit_code = proc.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            exit_code = 124
            stderr_raw = f"Execution timed out after {self.policy.timeout_seconds}s"
            self._terminate_process_tree(proc)
        except OSError as err:
            exit_code = 1
            stderr_raw = f"Sandbox execution failure: {str(err)[:256]}"

        duration = round((time.monotonic() - start) * 1000.0, 2)
        stdout_bounded = self._truncate_stream(stdout_raw)
        stderr_bounded = self._truncate_stream(stderr_raw)

        return SandboxExecutionResult(
            command=cmd,
            exit_code=exit_code,
            stdout=stdout_bounded,
            stderr=stderr_bounded,
            duration_ms=duration,
            timed_out=timed_out,
            runtime_used=runtime_name,
        )

    @staticmethod
    def _terminate_process_tree(proc: subprocess.Popen[str]) -> None:
        try:
            if hasattr(os, "killpg") and hasattr(os, "getpgid"):
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                time.sleep(0.05)
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            else:
                proc.kill()
        except OSError:
            pass

    @staticmethod
    def _truncate_stream(content: str) -> str:
        if len(content) <= MAX_OUTPUT_BYTES:
            return content
        half = (MAX_OUTPUT_BYTES - len(TRUNCATION_MARKER)) // 2
        return content[:half] + TRUNCATION_MARKER + content[-half:]


def run_benchmark_matrix(harness: ContainerSandboxHarness) -> list[tuple[str, bool, str]]:
    """Execute standard sandbox containment test matrix."""
    results: list[tuple[str, bool, str]] = []

    # 1. Safe computation
    res_safe = harness.run_python_code("print(sum(i * i for i in range(100)))")
    ok_safe = res_safe.exit_code == 0 and "328350" in res_safe.stdout
    results.append(("Safe Math Computation", ok_safe, f"{res_safe.duration_ms}ms"))

    # 2. Timeout containment
    res_timeout = harness.run_python_code("import time\ntime.sleep(10)")
    ok_timeout = res_timeout.timed_out and res_timeout.exit_code != 0
    results.append(("Infinite Loop Timeout Containment", ok_timeout, f"{res_timeout.duration_ms}ms"))

    # 3. Buffer overflow DOS prevention
    res_dos = harness.run_python_code("print('X' * 200000)")
    ok_dos = len(res_dos.stdout) <= MAX_OUTPUT_BYTES and TRUNCATION_MARKER in res_dos.stdout
    results.append(("Output Stream Buffer Bounding (CWE-400)", ok_dos, f"{len(res_dos.stdout)} bytes"))

    return results


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint for running audits and demo containment matrices."""
    parser = argparse.ArgumentParser(description="Ephemeral Rootless Container Sandbox Harness")
    parser.add_argument("--audit", action="store_true", help="Audit default security policy against CIS benchmarks")
    parser.add_argument("--demo", action="store_true", help="Run interactive sandbox containment matrix")
    parser.add_argument("--simulator", action="store_true", help="Force local simulator runtime instead of container")
    args = parser.parse_args(argv)

    policy = SandboxSecurityPolicy(timeout_seconds=0.15 if args.demo else 5.0)

    if args.audit or not args.demo:
        _print_audit_report(CISPolicyAuditor.audit(policy))
        # Always printed beside the policy audit, never instead of it. The policy score is
        # a property of the declaration; this one is a property of the run about to happen,
        # and reporting only the first is how this harness came to certify a containment it
        # was not applying.
        engine = ContainerSandboxHarness._resolve_engine(None, args.simulator)
        _print_enforcement_report(RuntimeEnforcementAuditor.audit(policy, engine))

    if args.demo:
        harness = ContainerSandboxHarness(policy=policy, force_simulator=args.simulator)
        _print_demo_header(harness.engine)
        matrix = run_benchmark_matrix(harness)
        _print_matrix_results(matrix)

    return 0


def _print_audit_report(report: CISAuditReport) -> None:
    print("==========================================================================")
    print("🔒 CIS ROOTLESS CONTAINER BENCHMARK AUDIT")
    print(f"Compliance Score: {report.score}% | Passed: {len(report.passed_controls)}/8")
    print("--------------------------------------------------------------------------")
    for ctrl in report.passed_controls:
        print(f"  [PASS] {ctrl}")
    for viol in report.violations:
        print(f"  [FAIL] {viol}")
    print("==========================================================================")


def _print_enforcement_report(report: EnforcementReport) -> None:
    print("==========================================================================")
    print(f"🧱 RUNTIME ENFORCEMENT ({report.engine})")
    enforced = len(report.controls) - len(report.unenforced)
    print(f"Enforced: {report.score}% | {enforced}/{len(report.controls)} controls applied by this engine")
    print("--------------------------------------------------------------------------")
    for control in report.controls:
        print(f"  [{'ENFORCED' if control.enforced else 'DECLARED'}] {control.control}")
        print(f"             {control.mechanism}")
    if report.unenforced:
        print("--------------------------------------------------------------------------")
        print("  The controls above marked DECLARED are in the policy and not in the run.")
    print("==========================================================================")


def _print_demo_header(engine: str) -> None:
    print("\n==========================================================================")
    print(f"🛡️  EPHEMERAL CONTAINER SANDBOX MATRIX (Runtime: {engine})")
    print("--------------------------------------------------------------------------")


def _print_matrix_results(matrix: list[tuple[str, bool, str]]) -> None:
    for name, passed, detail in matrix:
        status = "[CONTAINED]" if passed else "[FAILED]"
        print(f"  {status:<12} {name:<42} -> {detail}")
    print("==========================================================================")


if __name__ == "__main__":
    sys.exit(main())
