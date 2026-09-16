#!/usr/bin/env python3
"""CEGIS Debugging Workbench.

Demonstrates Counterexample-Guided Inductive Synthesis (CEGIS) for autonomous agent debugging.
Transforms trial-and-error code patching into a formal constraint-accumulation loop.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


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
    print(f"\n✅ CEGIS loop successfully converged in Round {runner.state.round_number}!")


if __name__ == "__main__":
    main()
