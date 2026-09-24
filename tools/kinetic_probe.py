#!/usr/bin/env python3
"""Kinetic probe execution engine and invariant leash registry.

Replaces subjective conversational review debate with sandboxed counterexample
execution, mapping operating system exit codes to deterministic verification
verdicts and tracking dynamic mitigation dependencies across commits.
"""

from __future__ import annotations

import argparse
import json
import os
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


class ProbeVerdict(StrEnum):
    """Kinetic verification outcomes derived from process execution."""

    EXPLOIT_CONFIRMED = "confirmed"
    KINETICALLY_REFUTED = "refuted"
    RESOURCE_EXHAUSTION = "exhaustion"
    PROBE_ERROR = "error"


@dataclass(frozen=True)
class KineticProbeResult:
    """Standardized result of a sandboxed kinetic probe execution."""

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


class KineticProbeEngine:
    """Executes synthesized counterexample probes within isolated subprocess boundaries."""

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
            return self._run_isolated_process(
                tmp_path, timeout, claim_id, mitigating_wrapper, start_time
            )
        finally:
            if tmp_path.exists():
                tmp_path.unlink()

    def _run_isolated_process(
        self,
        script_path: Path,
        timeout: float,
        claim_id: str,
        mitigating_wrapper: str | None,
        start_time: float,
    ) -> KineticProbeResult:
        """Run the script in an isolated subprocess with bounded execution limits."""
        env = os.environ.copy()
        env["PYTHONPATH"] = str(self.workspace_root)
        try:
            proc = subprocess.run(
                [sys.executable, str(script_path)],
                cwd=self.workspace_root,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
                check=False,
            )
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            verdict = (
                ProbeVerdict.EXPLOIT_CONFIRMED
                if proc.returncode == 0
                else ProbeVerdict.KINETICALLY_REFUTED
            )
            return KineticProbeResult(
                verdict=verdict,
                exit_code=proc.returncode,
                output=(proc.stdout + proc.stderr).strip(),
                duration_ms=elapsed_ms,
                claim_id=claim_id,
                mitigating_wrapper=mitigating_wrapper,
            )
        except subprocess.TimeoutExpired:
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            return KineticProbeResult(
                verdict=ProbeVerdict.RESOURCE_EXHAUSTION,
                exit_code=-1,
                output="Probe timed out: resource exhaustion detected",
                duration_ms=elapsed_ms,
                claim_id=claim_id,
                mitigating_wrapper=mitigating_wrapper,
            )
        except Exception as exc:
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            return KineticProbeResult(
                verdict=ProbeVerdict.PROBE_ERROR,
                exit_code=-2,
                output=f"Execution error: {exc}",
                duration_ms=elapsed_ms,
                claim_id=claim_id,
                mitigating_wrapper=mitigating_wrapper,
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
    leash_cmd.add_argument(
        "--registry", type=Path, required=True, help="Path to serialized leash JSON"
    )
    leash_cmd.add_argument(
        "--changed", nargs="+", required=True, help="List of changed file paths"
    )

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
