#!/usr/bin/env python3
"""Kinetic probe execution engine and invariant leash registry.

Runs each counterexample script in a child process with a timeout and an address-space
limit, and maps the exit code to a verdict. It also tracks mitigation dependencies across
commits. The child runs in its own POSIX process group, and a timeout kills that group.
The child is not a sandbox: it inherits the parent environment and has full file and
network access.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

DEFAULT_TIMEOUT_SECONDS: Final[float] = 5.0
DEFAULT_MEMORY_LIMIT_BYTES: Final[int] = 128 * 1024 * 1024

# The child runs this bootstrap with `python -c`. It sets the address-space limit before
# the probe runs, and it exits with 70 (EX_SOFTWARE in sysexits.h) on an uncaught
# exception, so that a crash cannot look like the deliberate exit 1 of a refuting probe.
# A preexec_fn is not used because it can deadlock a multithreaded parent at fork.
_PROBE_BOOTSTRAP: Final[str] = """\
import resource, runpy, sys, traceback
try:
    limit = int(sys.argv[1])
    resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    sys.argv = sys.argv[2:]
    runpy.run_path(sys.argv[0], run_name="__main__")
except SystemExit:
    raise
except BaseException:
    traceback.print_exc()
    sys.exit(70)
"""


class ProbeVerdict(StrEnum):
    """Kinetic verification outcomes derived from process execution."""

    EXPLOIT_CONFIRMED = "confirmed"
    KINETICALLY_REFUTED = "refuted"
    RESOURCE_EXHAUSTION = "exhaustion"
    INCONCLUSIVE = "inconclusive"
    PROBE_ERROR = "error"


# Only these two exit codes are verdicts on the claim. Any other exit (an uncaught
# exception, a MemoryError at the limit, a signal) means that the probe broke.
_EXIT_VERDICTS: Final[dict[int, ProbeVerdict]] = {
    0: ProbeVerdict.EXPLOIT_CONFIRMED,
    1: ProbeVerdict.KINETICALLY_REFUTED,
}


@dataclass(frozen=True)
class KineticProbeResult:
    """Standardized result of a kinetic probe execution."""

    verdict: ProbeVerdict
    exit_code: int
    output: str
    duration_ms: float
    claim_id: str = ""
    mitigating_wrapper: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert result to dictionary representation."""
        return {
            "verdict": self.verdict.value,
            "exit_code": self.exit_code,
            "output": self.output[:512],
            "duration_ms": round(self.duration_ms, 2),
            "claim_id": self.claim_id,
            "mitigating_wrapper": self.mitigating_wrapper,
        }


@dataclass(frozen=True)
class _ProcessOutcome:
    """Raw process outcome from a probe subprocess execution."""

    verdict: ProbeVerdict
    exit_code: int
    output: str


def _await_probe(proc: subprocess.Popen[str], timeout: float) -> _ProcessOutcome:
    """Wait for the probe, kill its process group on timeout, and map its exit code."""
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_process_group(proc)
        return _ProcessOutcome(
            verdict=ProbeVerdict.RESOURCE_EXHAUSTION,
            exit_code=-1,
            output="Probe timed out: resource exhaustion detected",
        )
    return _ProcessOutcome(
        verdict=_EXIT_VERDICTS.get(proc.returncode, ProbeVerdict.INCONCLUSIVE),
        exit_code=proc.returncode,
        output=(stdout + stderr).strip(),
    )


def _kill_process_group(proc: subprocess.Popen[str]) -> None:
    """Kill the process group that the probe leads, then reap the probe.

    The probe starts a new session, so its process group ID is its own PID. The probe is
    not reaped before the kill, so that ID cannot belong to another group.
    """
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGKILL)
    proc.wait()


class KineticProbeEngine:
    """Executes counterexample probes in a child process group with time and memory limits."""

    def __init__(self, workspace_root: Path | None = None) -> None:
        self.workspace_root: Final[Path] = (workspace_root or Path.cwd()).resolve()

    def execute_probe(
        self,
        probe_source: str,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        claim_id: str = "",
        mitigating_wrapper: str | None = None,
    ) -> KineticProbeResult:
        """Execute a probe script and evaluate the kinetic exit code."""
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as tmp:
            tmp.write(probe_source)
            tmp_path = Path(tmp.name)

        start_time = time.perf_counter()
        try:
            outcome = self._execute_subprocess(tmp_path, timeout)
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            return KineticProbeResult(
                verdict=outcome.verdict,
                exit_code=outcome.exit_code,
                output=outcome.output,
                duration_ms=elapsed_ms,
                claim_id=claim_id,
                mitigating_wrapper=mitigating_wrapper,
            )
        finally:
            if tmp_path.exists():
                tmp_path.unlink()

    def _execute_subprocess(self, script_path: Path, timeout: float) -> _ProcessOutcome:
        """Run the script in a new process group under the memory limit and the timeout."""
        env = os.environ.copy()
        env["PYTHONPATH"] = str(self.workspace_root)
        command = [
            sys.executable,
            "-c",
            _PROBE_BOOTSTRAP,
            str(DEFAULT_MEMORY_LIMIT_BYTES),
            str(script_path),
        ]
        try:
            with subprocess.Popen(
                command,
                cwd=self.workspace_root,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
                start_new_session=True,
            ) as proc:
                return _await_probe(proc, timeout)
        except OSError as exc:
            return _ProcessOutcome(
                verdict=ProbeVerdict.PROBE_ERROR,
                exit_code=-2,
                output=f"Execution error: {exc}",
            )


@dataclass
class InvariantLeashRegistry:
    """Tracks dynamic couplings between mitigated defects and their defensive perimeters."""

    _leashes: dict[str, list[str]] = field(default_factory=dict)

    def register_leash(self, defect_id: str, perimeter_files: Sequence[str]) -> None:
        """Bind a defect's mitigation to specific protective perimeter files."""
        self._leashes[defect_id] = [str(Path(p).as_posix()) for p in perimeter_files]

    def get_invalidated_defects(self, modified_files: Sequence[str]) -> list[str]:
        """Identify defects whose mitigating perimeter was modified in the current delta."""
        normalized_mods = {str(Path(p).as_posix()) for p in modified_files}
        return [
            defect_id
            for defect_id, perimeters in self._leashes.items()
            if any(p in normalized_mods for p in perimeters)
        ]

    def to_dict(self) -> dict[str, list[str]]:
        """Serialize registry state."""
        return dict(self._leashes)

    @classmethod
    def from_dict(cls, data: dict[str, list[str]]) -> InvariantLeashRegistry:
        """Deserialize registry state."""
        registry = cls()
        for k, v in data.items():
            registry.register_leash(k, v)
        return registry


def _build_parser() -> argparse.ArgumentParser:
    """Build CLI argument parser."""
    parser = argparse.ArgumentParser(
        description="Execute kinetic counterexample probes and evaluate invariant leashes."
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    run_cmd = subparsers.add_parser("run", help="Execute a kinetic probe script")
    run_cmd.add_argument("probe_file", type=Path, help="Path to Python probe script")
    run_cmd.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    run_cmd.add_argument("--claim-id", type=str, default="")

    leash_cmd = subparsers.add_parser("leash", help="Check invariant leash invalidations")
    leash_cmd.add_argument("--registry", type=Path, required=True, help="Path to serialized leash JSON")
    leash_cmd.add_argument("--changed", nargs="+", required=True, help="List of changed file paths")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for kinetic probe execution and leash checking."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.subcommand == "run":
        engine = KineticProbeEngine()
        code = args.probe_file.read_text(encoding="utf-8")
        result = engine.execute_probe(code, timeout=args.timeout, claim_id=args.claim_id)
        print(json.dumps(result.to_dict(), indent=2))
        return 0 if result.verdict == ProbeVerdict.EXPLOIT_CONFIRMED else 1

    if args.subcommand == "leash":
        data = json.loads(args.registry.read_text(encoding="utf-8"))
        registry = InvariantLeashRegistry.from_dict(data)
        invalidated = registry.get_invalidated_defects(args.changed)
        print(json.dumps({"invalidated_defects": invalidated}, indent=2))
        return 0 if not invalidated else 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
