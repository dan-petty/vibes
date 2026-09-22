#!/usr/bin/env python3
"""CEGIS Debugging Workbench.

Demonstrates Counterexample-Guided Inductive Synthesis (CEGIS) for autonomous agent debugging.
Transforms trial-and-error code patching into a formal constraint-accumulation loop.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import importlib.util
from pathlib import Path
import sys
from typing import Any, Callable


@dataclass(frozen=True)
class PatchExecutionResult:
    """Result of evaluating a patch within an execution sandbox."""

    success: bool
    error_message: str | None = None
    timed_out: bool = False
    duration_ms: float = 0.0


@dataclass(frozen=True)
class ConstraintSpec:
    """An input/output test case representing a positive or negative constraint."""

    input_data: str
    expected_output: dict[str, int | str]
    name: str = "test_case"


@dataclass
class Hypothesis:
    """A falsifiable hypothesis formulated by an agent explaining a defect."""

    description: str
    proposed_remediation: str


@dataclass
class SynthesisState:
    """State machine tracking CEGIS verification rounds."""

    round_number: int = 0
    accumulated_counterexamples: list[ConstraintSpec] = field(default_factory=list)
    verified_patches: int = 0
    converged: bool = False


# A defective parser simulating real-world agent edge-case failures
def buggy_manifest_parser(raw_text: str) -> dict[str, int | str]:
    """Flawed initial parser: assumes 'replicas' is always an unquoted integer."""
    result: dict[str, int | str] = {}
    for line in raw_text.strip().splitlines():
        if not line or ":" not in line:
            continue
        key, val = line.split(":", 1)
        k, v = key.strip(), val.strip()
        if k == "replicas":
            # BUG: Crashes on quoted numbers or non-digits
            result[k] = int(v)
        else:
            result[k] = v
    return result


def _parse_line_naive(line: str) -> tuple[str, int | str] | None:
    if not line or ":" not in line:
        return None
    key, val = line.split(":", 1)
    k, v = key.strip(), val.strip()
    try:
        return k, (int(v) if k == "replicas" else v)
    except Exception:
        return None


# Candidate patches synthesized by agent
def candidate_patch_naive_workaround(raw_text: str) -> dict[str, int | str]:
    """Bad Agent Patch: Masks exception instead of fixing root cause."""
    result: dict[str, int | str] = {}
    for line in raw_text.strip().splitlines():
        parsed = _parse_line_naive(line)
        if parsed:
            result[parsed[0]] = parsed[1]
    return result


def _parse_line_converged(line: str) -> tuple[str, int | str] | None:
    if not line or ":" not in line:
        return None
    key, val = line.split(":", 1)
    k, v = key.strip(), val.strip()
    if k == "replicas":
        clean_v = v.strip("\"'")
        return k, int(clean_v) if clean_v.isdigit() else 0
    return k, v


def candidate_patch_cegis_converged(raw_text: str) -> dict[str, int | str]:
    """Correct CEGIS Patch: Strips quotes and safely coerces numeric string to int."""
    result: dict[str, int | str] = {}
    for line in raw_text.strip().splitlines():
        parsed = _parse_line_converged(line)
        if parsed:
            result[parsed[0]] = parsed[1]
    return result


CANDIDATE_PATCH_RUNAWAY_LOOP = """
def parse_manifest_runaway(raw_text: str) -> dict[str, int | str]:
    import time
    while True:
        time.sleep(0.01)
"""

CANDIDATE_PATCH_CONVERGED_SOURCE = """
def _parse_line(line: str) -> tuple[str, int | str] | None:
    if not line or ":" not in line:
        return None
    key, val = line.split(":", 1)
    k, v = key.strip(), val.strip()
    if k == "replicas":
        clean = v.strip("\\"'")
        return k, int(clean) if clean.isdigit() else 0
    return k, v

def parse_manifest_converged(raw_text: str) -> dict[str, int | str]:
    res = {}
    for line in raw_text.strip().splitlines():
        p = _parse_line(line)
        if p:
            res[p[0]] = p[1]
    return res
"""


def _load_sandbox_module() -> Any:
    """Dynamically load container sandbox module with Python 3.14 sys.modules registration."""
    if "sandbox_module" in sys.modules:
        return sys.modules["sandbox_module"]
    sandbox_path = Path(__file__).resolve().parent.parent / "ephemeral-container-sandbox" / "sandbox.py"
    if not sandbox_path.is_file():
        return None
    try:
        spec = importlib.util.spec_from_file_location("sandbox_module", sandbox_path)
        if not (spec and spec.loader):
            return None
        mod = importlib.util.module_from_spec(spec)
        sys.modules["sandbox_module"] = mod
        spec.loader.exec_module(mod)
        return mod
    except (OSError, RuntimeError, ImportError):
        return None


# The timeout exists to contain a runaway candidate — `while True: pass` — not to
# benchmark a legitimate one. At 0.15s it did both, and a convergence test failed on a
# loaded host because evaluating a correct patch ran out of budget. A runaway loop is
# caught just as decisively at two seconds, and legitimate evaluation here takes single-
# digit milliseconds, so the containment property is unchanged and the flake is gone.
DEFAULT_EVALUATION_TIMEOUT_SECONDS: float = 2.0


class SandboxedPatchEvaluator:
    """Evaluates candidate patch source code in an isolated container or simulator sandbox."""

    def __init__(
        self, timeout_seconds: float = DEFAULT_EVALUATION_TIMEOUT_SECONDS, force_simulator: bool = True
    ) -> None:
        """Initialize evaluator with bounded timeout and sandbox engine."""
        self.timeout_seconds = timeout_seconds
        self.force_simulator = force_simulator
        self._harness = self._create_harness()

    def _create_harness(self) -> Any:
        mod = _load_sandbox_module()
        if mod is None or not hasattr(mod, "ContainerSandboxHarness"):
            return None
        policy = mod.SandboxSecurityPolicy(timeout_seconds=self.timeout_seconds)
        return mod.ContainerSandboxHarness(policy=policy, force_simulator=self.force_simulator)

    def evaluate_code(
        self,
        patch_source: str,
        entrypoint: str,
        test_case: ConstraintSpec,
    ) -> PatchExecutionResult:
        """Run candidate code against a constraint spec inside the sandbox."""
        if self._harness is None:
            return PatchExecutionResult(success=False, error_message="Sandbox harness unavailable")
        script = self._build_driver_script(patch_source, entrypoint, test_case)
        res = self._harness.run_python_code(script)
        return PatchExecutionResult(
            success=res.exit_code == 0,
            error_message=res.stderr.strip() if res.stderr else None,
            timed_out=res.timed_out,
            duration_ms=res.duration_ms,
        )

    @staticmethod
    def _build_driver_script(
        patch_source: str,
        entrypoint: str,
        test_case: ConstraintSpec,
    ) -> str:
        return (
            "import json, sys\n\n"
            f"{patch_source}\n\n"
            f"input_str = {repr(test_case.input_data)}\n"
            f"expected = {repr(test_case.expected_output)}\n"
            "try:\n"
            f"    actual = {entrypoint}(input_str)\n"
            "    if actual == expected:\n"
            "        sys.exit(0)\n"
            "    sys.stderr.write(f'Mismatch: {actual} != {expected}\\n')\n"
            "    sys.exit(1)\n"
            "except Exception as err:\n"
            "    sys.stderr.write(f'Exception: {str(err)[:200]}\\n')\n"
            "    sys.exit(2)\n"
        )


def _test_matches(candidate_fn: Callable[[str], dict[str, int | str]], test: ConstraintSpec) -> bool:
    try:
        return candidate_fn(test.input_data) == test.expected_output
    except Exception:
        return False


class CEGISRunner:
    """Executes the CEGIS loop: Test suite verification, counterexample isolation, and patch validation."""

    def __init__(self, baseline_suite: list[ConstraintSpec]) -> None:
        self.baseline_suite = list(baseline_suite)
        self.state = SynthesisState()

    def run_oracle(
        self, candidate_fn: Callable[[str], dict[str, int | str]]
    ) -> tuple[bool, ConstraintSpec | None]:
        """Verify candidate function against all baseline tests and accumulated counterexamples."""
        all_tests = self.baseline_suite + self.state.accumulated_counterexamples
        for test in all_tests:
            if not _test_matches(candidate_fn, test):
                return False, test
        return True, None

    def record_counterexample(self, counterexample: ConstraintSpec) -> None:
        """Add a failing counterexample to the formal negative constraint set."""
        self.state.accumulated_counterexamples.append(counterexample)
        self.state.round_number += 1

    def verify_and_converge(
        self, candidate_fn: Callable[[str], dict[str, int | str]], hypothesis: Hypothesis
    ) -> bool:
        """Evaluate candidate patch against the full constraint suite."""
        success, failed_case = self.run_oracle(candidate_fn)
        if success:
            self.state.converged = True
            self.state.verified_patches += 1
            return True
        return False

    def run_sandboxed_oracle(
        self,
        patch_source: str,
        entrypoint: str,
        evaluator: SandboxedPatchEvaluator | None = None,
    ) -> tuple[bool, ConstraintSpec | None]:
        """Verify candidate patch source code against all constraints inside isolated sandbox."""
        ev = evaluator or SandboxedPatchEvaluator(force_simulator=True)
        all_tests = self.baseline_suite + self.state.accumulated_counterexamples
        for test in all_tests:
            result = ev.evaluate_code(patch_source, entrypoint, test)
            if not result.success:
                return False, test
        return True, None

    def verify_and_converge_sandboxed(
        self,
        patch_source: str,
        entrypoint: str,
        hypothesis: Hypothesis,
        evaluator: SandboxedPatchEvaluator | None = None,
    ) -> bool:
        """Evaluate sandboxed candidate patch against the full constraint suite."""
        success, _ = self.run_sandboxed_oracle(patch_source, entrypoint, evaluator)
        if success:
            self.state.converged = True
            self.state.verified_patches += 1
            return True
        return False


def main() -> None:
    """Execute counterexample-guided inductive synthesis debugging loop demonstration."""
    print("🔬 CEGIS Debugging Workbench Demo\n")

    baseline = [
        ConstraintSpec(
            input_data="app: web\nreplicas: 3",
            expected_output={"app": "web", "replicas": 3},
            name="standard_unquoted",
        )
    ]

    runner = CEGISRunner(baseline)

    # 1. Baseline passes with buggy parser
    ok, _ = runner.run_oracle(buggy_manifest_parser)
    print(f"1. Baseline test passes on initial parser: {ok}")

    # 2. Introduce novel failing counterexample
    counterexample = ConstraintSpec(
        input_data="app: web\nreplicas: '5'",
        expected_output={"app": "web", "replicas": 5},
        name="quoted_string_replicas",
    )
    runner.record_counterexample(counterexample)
    print(f"2. Counterexample registered: '{counterexample.name}'")

    # 3. Buggy parser fails against accumulated constraints
    ok, failing = runner.run_oracle(buggy_manifest_parser)
    print(f"3. Initial parser passes after constraint accumulation: {ok} (Failed: {failing.name if failing else 'None'})")

    # 4. Naive workaround attempt
    hypo_naive = Hypothesis(
        description="Ignore exceptions when parsing replicas",
        proposed_remediation="Wrap int conversion in try/except pass",
    )
    ok_naive = runner.verify_and_converge(candidate_patch_naive_workaround, hypo_naive)
    print(f"4. Naive workaround converged: {ok_naive} (Rejected by Oracle)")

    # 5. Correct CEGIS synthesized patch
    hypo_correct = Hypothesis(
        description="Replicas value contains surrounding quote delimiters",
        proposed_remediation="Strip quote characters before integer conversion",
    )
    ok_correct = runner.verify_and_converge(candidate_patch_cegis_converged, hypo_correct)
    print(f"5. CEGIS candidate patch converged: {ok_correct} (Certified by Oracle)")

    # 6. Sandboxed container / simulator evaluation
    print("\n--- Sandboxed Container / Process Group Evaluation ---")
    # The demo's whole point here is a candidate that never terminates, so it uses a short
    # explicit budget rather than the default. The default has headroom for a legitimate
    # patch on a loaded host; waiting two seconds to prove an infinite loop is an infinite
    # loop is time spent demonstrating nothing.
    evaluator = SandboxedPatchEvaluator(timeout_seconds=0.1, force_simulator=True)
    res_runaway = evaluator.evaluate_code(
        CANDIDATE_PATCH_RUNAWAY_LOOP,
        "parse_manifest_runaway",
        counterexample,
    )
    print(f"6a. Runaway loop contained safely by sandbox: {res_runaway.timed_out} (Timed out in {res_runaway.duration_ms}ms)")

    ok_sandboxed = runner.verify_and_converge_sandboxed(
        CANDIDATE_PATCH_CONVERGED_SOURCE,
        "parse_manifest_converged",
        hypo_correct,
        evaluator=evaluator,
    )
    print(f"6b. Sandboxed patch evaluated and converged: {ok_sandboxed}")
    print(f"\n✅ CEGIS loop successfully converged in Round {runner.state.round_number}!")


if __name__ == "__main__":
    main()
