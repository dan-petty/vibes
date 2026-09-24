#!/usr/bin/env python3
"""Counterexample-Guided Inductive Synthesis (CEGIS) Engine & Invariant Repair Oracle.

Implements formal counterexample-guided inductive synthesis for autonomous AI agent
feature development and self-improvement loops. Replaces unconstrained conversational
"try-again" prompting with mathematically grounded negative constraint accumulation,
monotonic convergence verification, cycle oscillation detection, and latent regression
prevention (preventing latent security and architectural degradation).
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import ipaddress
import json
import re
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from radon.visitors import ComplexityVisitor

ENGINE_VERSION: Final[str] = "v1.0.0"
IP_PATTERN: Final[re.Pattern[str]] = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")

NESTING_TYPES: Final[tuple[type[ast.AST], ...]] = (
    ast.If,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.With,
    ast.AsyncWith,
    ast.Try,
    ast.Match,
)

SARIF_RULES: Final[dict[str, dict[str, Any]]] = {
    "CEGIS001": {
        "id": "CEGIS001",
        "name": "InvariantComplexityExceeded",
        "shortDescription": {"text": "Function McCabe cyclomatic complexity exceeds configured ceiling."},
        "defaultConfiguration": {"level": "error"},
        "helpUri": "https://github.com/dan-petty/vibes",
    },
    "CEGIS002": {
        "id": "CEGIS002",
        "name": "ExcessiveNestingDepth",
        "shortDescription": {"text": "Function statement nesting depth exceeds allowable threshold."},
        "defaultConfiguration": {"level": "error"},
        "helpUri": "https://github.com/dan-petty/vibes",
    },
    "CEGIS003": {
        "id": "CEGIS003",
        "name": "ParameterCardinalityExceeded",
        "shortDescription": {"text": "Function parameter count exceeds maximum allowable arguments."},
        "defaultConfiguration": {"level": "warning"},
        "helpUri": "https://github.com/dan-petty/vibes",
    },
    "CEGIS004": {
        "id": "CEGIS004",
        "name": "ZeroTrustEgressViolation",
        "shortDescription": {"text": "Private RFC 1918 or RFC 4193 IP address detected in source code."},
        "defaultConfiguration": {"level": "error"},
        "helpUri": "https://github.com/dan-petty/vibes",
    },
    "CEGIS005": {
        "id": "CEGIS005",
        "name": "AssertionSprawlDetected",
        "shortDescription": {"text": "Linear consecutive assert statements detected without structural tuple consolidation."},
        "defaultConfiguration": {"level": "warning"},
        "helpUri": "https://github.com/dan-petty/vibes",
    },
    "CEGIS006": {
        "id": "CEGIS006",
        "name": "RepairCycleOscillationDetected",
        "shortDescription": {"text": "Iterative synthesis entered a non-convergent oscillatory cycle between candidate states."},
        "defaultConfiguration": {"level": "error"},
        "helpUri": "https://github.com/dan-petty/vibes",
    },
    "CEGIS007": {
        "id": "CEGIS007",
        "name": "LatentInvariantRegression",
        "shortDescription": {"text": "Synthesis repair introduced a regression on a previously satisfied invariant property."},
        "defaultConfiguration": {"level": "error"},
        "helpUri": "https://github.com/dan-petty/vibes",
    },
    "CEGIS008": {
        "id": "CEGIS008",
        "name": "NegativeConstraintViolation",
        "shortDescription": {"text": "Candidate program reproduced a previously registered negative counterexample pattern."},
        "defaultConfiguration": {"level": "error"},
        "helpUri": "https://github.com/dan-petty/vibes",
    },
}


class CEGISPreset(StrEnum):
    """Calibrated invariant presets for CEGIS verification."""

    STANDARD = "standard"
    STRICT = "strict"
    PEDANTIC = "pedantic"


class ConvergenceStatus(StrEnum):
    """Lifecycle convergence state of a synthesis trajectory."""

    CONVERGED = "converged"
    CONVERGING = "converging"
    OSCILLATING = "oscillating"
    DIVERGING = "diverging"
    BUDGET_EXHAUSTED = "budget_exhausted"
    DEGRADED = "degraded"


@dataclass(frozen=True)
class InvariantThresholds:
    """Configured numerical and structural bounds for CEGIS verification."""

    max_complexity: int
    max_depth: int
    max_params: int
    forbid_assertion_sprawl: bool = True
    enforce_zero_trust_ips: bool = True


PRESET_THRESHOLDS: Final[dict[CEGISPreset, InvariantThresholds]] = {
    CEGISPreset.STANDARD: InvariantThresholds(max_complexity=10, max_depth=5, max_params=4),
    CEGISPreset.STRICT: InvariantThresholds(max_complexity=6, max_depth=3, max_params=4),
    CEGISPreset.PEDANTIC: InvariantThresholds(max_complexity=4, max_depth=2, max_params=3),
}


@dataclass(frozen=True)
class Counterexample:
    """Formal counterexample witnessing an invariant violation."""

    rule_id: str
    location: str
    message: str
    violating_pattern: str
    counterexample_hash: str
    line_number: int = 1


@dataclass(frozen=True)
class NegativeConstraint:
    """Accumulated negative constraint forbidding a specific failure pattern."""

    constraint_id: str
    rule_id: str
    counterexample_hash: str
    predicate_description: str
    introduced_at_step: int


@dataclass(frozen=True)
class CandidateProgram:
    """Candidate program snapshot generated during a synthesis step."""

    candidate_id: str
    source_code: str
    step_index: int
    ast_hash: str


@dataclass(frozen=True)
class TrajectoryStep:
    """Recorded metrics and findings for a single iteration in the synthesis trajectory."""

    step_index: int
    candidate_id: str
    ast_hash: str
    passed: bool
    violations: tuple[Counterexample, ...]
    active_constraints_count: int
    max_complexity: int
    max_depth: int


@dataclass(frozen=True)
class CEGISResult:
    """Comprehensive outcome of a CEGIS execution or trajectory audit."""

    status: ConvergenceStatus
    total_iterations: int
    final_candidate: CandidateProgram | None
    trajectory: tuple[TrajectoryStep, ...]
    accumulated_constraints: tuple[NegativeConstraint, ...]
    detected_cycles: tuple[tuple[int, int], ...]
    latent_regressions: tuple[str, ...]
    findings: tuple[Counterexample, ...]


def _compute_hash(content: str) -> str:
    """Compute hexadecimal SHA-256 hash of text."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _ast_normalized_hash(source_code: str) -> str:
    """Return SHA-256 digest of normalized AST structure, ignoring trivia."""
    try:
        tree = ast.parse(source_code)
        dumped = ast.dump(tree, include_attributes=False)
        return _compute_hash(dumped)
    except SyntaxError:
        return _compute_hash(source_code)


def _compute_cyclomatic_complexity(node: ast.AST) -> int:
    """Compute radon McCabe complexity for AST node."""
    visitor = ComplexityVisitor.from_ast(node)
    blocks = list(visitor.functions) + list(visitor.classes)
    return max((b.complexity for b in blocks), default=visitor.total_complexity)


def _compute_nesting_depth(node: ast.AST) -> int:
    """Compute deepest nesting run of compound blocks in AST node."""
    def walk(current: ast.AST, depth: int) -> int:
        deeper = depth + 1 if isinstance(current, NESTING_TYPES) else depth
        return max((walk(c, deeper) for c in ast.iter_child_nodes(current)), default=deeper)

    return max((walk(s, 1) for s in getattr(node, "body", [])), default=0)


def _count_parameters(func: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    """Count declared parameters on function, excluding self/cls receiver."""
    args = func.args
    pos_count = len(args.posonlyargs) + len(args.args)
    if args.args and args.args[0].arg in ("self", "cls"):
        pos_count -= 1
    kw_count = len(args.kwonlyargs)
    vararg_count = 1 if args.vararg is not None else 0
    kwarg_count = 1 if args.kwarg is not None else 0
    return pos_count + kw_count + vararg_count + kwarg_count


def _is_private_ip(candidate: str) -> bool:
    """Return True if candidate string represents a private IP address."""
    try:
        ip = ipaddress.ip_address(candidate)
        return ip.is_private
    except ValueError:
        return False


def _check_string_constants_for_private_ips(tree: ast.AST, filename: str) -> list[Counterexample]:
    """Scan string constants for private RFC 1918/RFC 4193 IP addresses."""
    violations: list[Counterexample] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            for match in IP_PATTERN.findall(node.value):
                if _is_private_ip(match):
                    line = getattr(node, "lineno", 1)
                    loc = f"{filename}:{line}"
                    msg = f"Zero-trust violation: private IP address '{match}' in string literal"
                    h = _compute_hash(f"CEGIS004:{loc}:{match}")
                    violations.append(
                        Counterexample(
                            rule_id="CEGIS004",
                            location=loc,
                            message=msg,
                            violating_pattern=match,
                            counterexample_hash=h,
                            line_number=line,
                        )
                    )
    return violations


def _check_assertion_sprawl(func: ast.FunctionDef | ast.AsyncFunctionDef, filename: str) -> list[Counterexample]:
    """Detect consecutive linear assert statements in test functions."""
    if not func.name.startswith("test_"):
        return []
    consecutive = 0
    first_line = func.lineno
    for stmt in func.body:
        if isinstance(stmt, ast.Assert):
            consecutive += 1
            if consecutive == 2:
                first_line = stmt.lineno
        else:
            consecutive = 0
        if consecutive >= 3:
            loc = f"{filename}:{first_line}"
            msg = f"Assertion sprawl in '{func.name}': 3+ consecutive asserts without tuple consolidation"
            h = _compute_hash(f"CEGIS005:{loc}:{func.name}")
            return [
                Counterexample(
                    rule_id="CEGIS005",
                    location=loc,
                    message=msg,
                    violating_pattern="consecutive_asserts",
                    counterexample_hash=h,
                    line_number=first_line,
                )
            ]
    return []


def _check_function_invariants(
    func: ast.FunctionDef | ast.AsyncFunctionDef,
    thresholds: InvariantThresholds,
    filename: str,
) -> list[Counterexample]:
    """Verify structural complexity and parameter bounds on a function node."""
    violations: list[Counterexample] = []
    loc = f"{filename}:{func.lineno}"

    cc = _compute_cyclomatic_complexity(func)
    if cc > thresholds.max_complexity:
        msg = f"Cyclomatic complexity M={cc} exceeds ceiling M<={thresholds.max_complexity} in '{func.name}'"
        h = _compute_hash(f"CEGIS001:{loc}:{func.name}:{cc}")
        violations.append(
            Counterexample("CEGIS001", loc, msg, f"complexity_{cc}", h, func.lineno)
        )

    depth = _compute_nesting_depth(func)
    if depth > thresholds.max_depth:
        msg = f"Nesting depth D={depth} exceeds ceiling D<={thresholds.max_depth} in '{func.name}'"
        h = _compute_hash(f"CEGIS002:{loc}:{func.name}:{depth}")
        violations.append(
            Counterexample("CEGIS002", loc, msg, f"depth_{depth}", h, func.lineno)
        )

    params = _count_parameters(func)
    if params > thresholds.max_params:
        msg = f"Parameter count P={params} exceeds ceiling P<={thresholds.max_params} in '{func.name}'"
        h = _compute_hash(f"CEGIS003:{loc}:{func.name}:{params}")
        violations.append(
            Counterexample("CEGIS003", loc, msg, f"params_{params}", h, func.lineno)
        )

    if thresholds.forbid_assertion_sprawl:
        violations.extend(_check_assertion_sprawl(func, filename))

    return violations


class ASTInvariantVerifier:
    """Verifies Python source code against architectural invariants and generates counterexamples."""

    def __init__(self, thresholds: InvariantThresholds | None = None) -> None:
        """Initialize verifier with optional invariant thresholds."""
        self.thresholds = thresholds or PRESET_THRESHOLDS[CEGISPreset.STRICT]

    def verify_source(self, source_code: str, filename: str = "inline.py") -> tuple[list[Counterexample], int, int]:
        """Verify code against invariants, returning violations, max complexity, and max depth."""
        try:
            tree = ast.parse(source_code, filename=filename)
        except SyntaxError as err:
            h = _compute_hash(f"SYNTAX_ERR:{err.lineno or 1}:{err.msg}")
            ce = Counterexample(
                rule_id="CEGIS001",
                location=f"{filename}:{err.lineno or 1}",
                message=f"SyntaxError parsing candidate: {err.msg}",
                violating_pattern="syntax_error",
                counterexample_hash=h,
                line_number=err.lineno or 1,
            )
            return [ce], 99, 99

        violations: list[Counterexample] = []
        max_cc = 0
        max_d = 0

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                func_cc = _compute_cyclomatic_complexity(node)
                func_d = _compute_nesting_depth(node)
                max_cc = max(max_cc, func_cc)
                max_d = max(max_d, func_d)
                violations.extend(_check_function_invariants(node, self.thresholds, filename))

        if self.thresholds.enforce_zero_trust_ips:
            violations.extend(_check_string_constants_for_private_ips(tree, filename))

        return violations, max_cc, max_d


class NegativeConstraintAccumulator:
    """Accumulates and manages negative constraints to prevent repeat failure modes."""

    def __init__(self) -> None:
        """Initialize empty accumulator."""
        self._constraints: dict[str, NegativeConstraint] = {}

    @property
    def constraints(self) -> tuple[NegativeConstraint, ...]:
        """Return tuple of accumulated negative constraints."""
        return tuple(self._constraints.values())

    def register_counterexample(self, ce: Counterexample, step_index: int) -> NegativeConstraint:
        """Register a counterexample as a permanent negative constraint."""
        if ce.counterexample_hash in self._constraints:
            return self._constraints[ce.counterexample_hash]

        cid = f"NC-{len(self._constraints) + 1:03d}"
        pred = f"Forbid {ce.rule_id} at {ce.location}: {ce.message}"
        nc = NegativeConstraint(
            constraint_id=cid,
            rule_id=ce.rule_id,
            counterexample_hash=ce.counterexample_hash,
            predicate_description=pred,
            introduced_at_step=step_index,
        )
        self._constraints[ce.counterexample_hash] = nc
        return nc

    def check_for_repeat_violation(self, violations: Sequence[Counterexample]) -> list[Counterexample]:
        """Identify if any current violation matches an existing registered negative constraint."""
        repeats: list[Counterexample] = []
        for v in violations:
            if v.counterexample_hash in self._constraints:
                c = self._constraints[v.counterexample_hash]
                loc = v.location
                msg = f"Negative constraint violation {c.constraint_id}: repeated {v.rule_id} at {loc}"
                h = _compute_hash(f"CEGIS008:{c.constraint_id}:{v.counterexample_hash}")
                repeats.append(
                    Counterexample("CEGIS008", loc, msg, c.constraint_id, h, v.line_number)
                )
        return repeats


class ConvergenceOracle:
    """Analyzes synthesis trajectories for convergence, cycle oscillation, and latent degradation."""

    @staticmethod
    def detect_cycles(steps: Sequence[TrajectoryStep]) -> list[tuple[int, int]]:
        """Identify pairs of step indices forming state cycles (identical AST hash)."""
        seen: dict[str, int] = {}
        cycles: list[tuple[int, int]] = []
        for step in steps:
            if step.ast_hash in seen:
                prev_idx = seen[step.ast_hash]
                cycles.append((prev_idx, step.step_index))
            else:
                seen[step.ast_hash] = step.step_index
        return cycles

    @staticmethod
    def detect_latent_regressions(steps: Sequence[TrajectoryStep]) -> list[str]:
        """Detect invariant rules that passed in step t-1 but failed in step t."""
        if len(steps) < 2:
            return []
        regressions: list[str] = []
        for i in range(1, len(steps)):
            prev_rules = {v.rule_id for v in steps[i - 1].violations}
            curr_rules = {v.rule_id for v in steps[i].violations}
            # Latent regression: rule was clean at t-1 but introduced at t while other errors fixed
            new_rules = curr_rules - prev_rules
            if new_rules and len(prev_rules) > 0:
                regressions.extend(sorted(new_rules))
        return regressions

    @classmethod
    def evaluate(cls, steps: Sequence[TrajectoryStep], max_iterations: int) -> tuple[ConvergenceStatus, list[tuple[int, int]], list[str]]:
        """Evaluate overall trajectory convergence state."""
        if not steps:
            return ConvergenceStatus.BUDGET_EXHAUSTED, [], []

        latest = steps[-1]
        cycles = cls.detect_cycles(steps)
        regressions = cls.detect_latent_regressions(steps)

        if latest.passed:
            return ConvergenceStatus.CONVERGED, cycles, regressions

        if cycles:
            return ConvergenceStatus.OSCILLATING, cycles, regressions

        if regressions and len(latest.violations) >= len(steps[0].violations):
            return ConvergenceStatus.DEGRADED, cycles, regressions

        if len(steps) >= max_iterations:
            return ConvergenceStatus.BUDGET_EXHAUSTED, cycles, regressions

        # Check descent vs divergence
        if len(steps) >= 3:
            v_counts = [len(s.violations) for s in steps[-3:]]
            if v_counts[0] < v_counts[1] < v_counts[2]:
                return ConvergenceStatus.DIVERGING, cycles, regressions

        return ConvergenceStatus.CONVERGING, cycles, regressions


class CEGISEngine:
    """Orchestrates counterexample-guided inductive synthesis loops and trajectory audits."""

    def __init__(self, preset: CEGISPreset = CEGISPreset.STRICT) -> None:
        """Initialize engine with specified preset."""
        self.preset = preset
        self.thresholds = PRESET_THRESHOLDS[preset]
        self.verifier = ASTInvariantVerifier(self.thresholds)

    def audit_single(self, source_code: str, filename: str = "inline.py") -> CEGISResult:
        """Perform a single-pass verification audit and extract counterexamples."""
        ast_hash = _ast_normalized_hash(source_code)
        violations, max_cc, max_d = self.verifier.verify_source(source_code, filename)
        step = TrajectoryStep(
            step_index=0,
            candidate_id="cand_0",
            ast_hash=ast_hash,
            passed=len(violations) == 0,
            violations=tuple(violations),
            active_constraints_count=0,
            max_complexity=max_cc,
            max_depth=max_d,
        )
        status = ConvergenceStatus.CONVERGED if step.passed else ConvergenceStatus.CONVERGING
        accumulator = NegativeConstraintAccumulator()
        for v in violations:
            accumulator.register_counterexample(v, 0)

        cand = CandidateProgram("cand_0", source_code, 0, ast_hash)
        return CEGISResult(
            status=status,
            total_iterations=1,
            final_candidate=cand,
            trajectory=(step,),
            accumulated_constraints=accumulator.constraints,
            detected_cycles=(),
            latent_regressions=(),
            findings=tuple(violations),
        )

    def audit_trajectory(
        self,
        trajectory_sources: Sequence[str],
        filename: str = "inline.py",
        max_iterations: int = 10,
    ) -> CEGISResult:
        """Audit a multi-step synthesis trajectory for convergence, cycles, and regressions."""
        steps: list[TrajectoryStep] = []
        accumulator = NegativeConstraintAccumulator()
        all_findings: list[Counterexample] = []
        last_cand: CandidateProgram | None = None

        for idx, src in enumerate(trajectory_sources):
            ast_h = _ast_normalized_hash(src)
            last_cand = CandidateProgram(f"cand_{idx}", src, idx, ast_h)
            violations, max_cc, max_d = self.verifier.verify_source(src, filename)

            # Check if repeat of earlier negative constraint
            repeat_violations = accumulator.check_for_repeat_violation(violations)
            effective_violations = list(violations) + repeat_violations

            for v in effective_violations:
                accumulator.register_counterexample(v, idx)

            step = TrajectoryStep(
                step_index=idx,
                candidate_id=f"cand_{idx}",
                ast_hash=ast_h,
                passed=len(effective_violations) == 0,
                violations=tuple(effective_violations),
                active_constraints_count=len(accumulator.constraints),
                max_complexity=max_cc,
                max_depth=max_d,
            )
            steps.append(step)
            all_findings.extend(effective_violations)

        status, cycles, regressions = ConvergenceOracle.evaluate(steps, max_iterations)

        return CEGISResult(
            status=status,
            total_iterations=len(steps),
            final_candidate=last_cand,
            trajectory=tuple(steps),
            accumulated_constraints=accumulator.constraints,
            detected_cycles=tuple(cycles),
            latent_regressions=tuple(regressions),
            findings=tuple(all_findings),
        )

    def run_synthesis_loop(
        self,
        initial_source: str,
        synthesizer_fn: Callable[[str, Sequence[NegativeConstraint]], str],
        max_iterations: int = 5,
        filename: str = "inline.py",
    ) -> CEGISResult:
        """Run inductive synthesis loop using synthesizer callback until convergence."""
        sources: list[str] = [initial_source]
        current_source = initial_source
        accumulator = NegativeConstraintAccumulator()

        for _ in range(max_iterations):
            violations, _, _ = self.verifier.verify_source(current_source, filename)
            if not violations:
                break
            for v in violations:
                accumulator.register_counterexample(v, len(sources) - 1)
            next_source = synthesizer_fn(current_source, accumulator.constraints)
            sources.append(next_source)
            current_source = next_source

        return self.audit_trajectory(sources, filename, max_iterations)


def export_sarif(result: CEGISResult, filepath: str) -> dict[str, Any]:
    """Export CEGIS findings to OASIS SARIF 2.1.0 formatted dictionary."""
    results: list[dict[str, Any]] = []
    for f in result.findings:
        rule_def = SARIF_RULES.get(f.rule_id, {
            "id": f.rule_id,
            "name": "CEGISInvariantViolation",
            "shortDescription": {"text": f.message},
            "defaultConfiguration": {"level": "error"},
        })
        parts = f.location.split(":")
        file_target = parts[0] if parts else filepath
        line_num = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else f.line_number

        results.append({
            "ruleId": f.rule_id,
            "level": rule_def["defaultConfiguration"]["level"],
            "message": {"text": f.message},
            "locations": [
                {
                    "physicalLocation": {
                        "artifactLocation": {"uri": file_target},
                        "region": {"startLine": max(1, line_num)},
                    }
                }
            ],
            "properties": {
                "violatingPattern": f.violating_pattern,
                "counterexampleHash": f.counterexample_hash,
            },
        })

    return {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "vibes-cegis-engine",
                        "informationUri": "https://github.com/dan-petty/vibes",
                        "version": ENGINE_VERSION,
                        "rules": list(SARIF_RULES.values()),
                    }
                },
                "results": results,
            }
        ],
    }


def export_json(result: CEGISResult) -> str:
    """Export CEGIS result and trajectory telemetry as JSON string."""
    data = {
        "engine_version": ENGINE_VERSION,
        "status": result.status.value,
        "total_iterations": result.total_iterations,
        "detected_cycles": result.detected_cycles,
        "latent_regressions": result.latent_regressions,
        "accumulated_constraints_count": len(result.accumulated_constraints),
        "trajectory": [
            {
                "step_index": s.step_index,
                "candidate_id": s.candidate_id,
                "passed": s.passed,
                "violations_count": len(s.violations),
                "max_complexity": s.max_complexity,
                "max_depth": s.max_depth,
            }
            for s in result.trajectory
        ],
        "findings": [
            {
                "rule_id": f.rule_id,
                "location": f.location,
                "message": f.message,
                "line_number": f.line_number,
                "pattern": f.violating_pattern,
            }
            for f in result.findings
        ],
    }
    return json.dumps(data, indent=2)


def format_markdown_report(result: CEGISResult) -> str:
    """Format human-readable Markdown summary report for CEGIS execution."""
    lines = [
        "# Counterexample-Guided Inductive Synthesis (CEGIS) Report",
        "",
        f"- **Convergence Status**: `{result.status.value.upper()}`",
        f"- **Total Iterations**: {result.total_iterations}",
        f"- **Active Constraints**: {len(result.accumulated_constraints)}",
        f"- **Cycles Detected**: {len(result.detected_cycles)}",
        f"- **Latent Regressions**: {len(result.latent_regressions)}",
        "",
        "## Trajectory History",
        "",
        "| Step | Candidate | Status | Violations | Max M | Max Depth |",
        "| :--- | :--- | :--- | :--- | :--- | :--- |",
    ]
    for s in result.trajectory:
        stat = "✓ PASS" if s.passed else f"✗ FAIL ({len(s.violations)})"
        lines.append(
            f"| {s.step_index} | `{s.candidate_id}` | {stat} | {len(s.violations)} | {s.max_complexity} | {s.max_depth} |"
        )
    lines.append("")

    if result.accumulated_constraints:
        lines.extend([
            "## Accumulated Negative Constraints",
            "",
            "| ID | Rule | Step | Predicate Description |",
            "| :--- | :--- | :--- | :--- |",
        ])
        for c in result.accumulated_constraints:
            lines.append(
                f"| `{c.constraint_id}` | `{c.rule_id}` | {c.introduced_at_step} | {c.predicate_description} |"
            )
        lines.append("")

    return "\n".join(lines)


def _build_argument_parser() -> argparse.ArgumentParser:
    """Construct argument parser for CLI execution."""
    parser = argparse.ArgumentParser(
        prog="cegis_engine.py",
        description="Counterexample-Guided Inductive Synthesis & Invariant Repair Oracle.",
    )
    parser.add_argument(
        "target",
        nargs="?",
        default=None,
        help="Path to Python file to verify against invariants.",
    )
    parser.add_argument(
        "--preset",
        choices=[p.value for p in CEGISPreset],
        default=CEGISPreset.STRICT.value,
        help="Invariant verification preset (default: strict).",
    )
    parser.add_argument(
        "--export-sarif",
        metavar="PATH",
        help="Export findings as SARIF 2.1.0 JSON to PATH.",
    )
    parser.add_argument(
        "--export-json",
        metavar="PATH",
        help="Export execution result as JSON to PATH.",
    )
    parser.add_argument(
        "--export-md",
        metavar="PATH",
        help="Export execution report as Markdown to PATH.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Execute main command line routine."""
    parser = _build_argument_parser()
    args = parser.parse_args(argv)

    if not args.target:
        print("No target file specified. Use --help for usage.", file=sys.stderr)
        return 1

    target_path = Path(args.target)
    if not target_path.is_file():
        print(f"Error: Target file '{target_path}' not found.", file=sys.stderr)
        return 1

    source_code = target_path.read_text(encoding="utf-8")
    preset = CEGISPreset(args.preset)
    engine = CEGISEngine(preset=preset)
    result = engine.audit_single(source_code, str(target_path))

    if args.export_sarif:
        sarif_data = export_sarif(result, str(target_path))
        Path(args.export_sarif).write_text(json.dumps(sarif_data, indent=2), encoding="utf-8")

    if args.export_json:
        json_data = export_json(result)
        Path(args.export_json).write_text(json_data, encoding="utf-8")

    if args.export_md:
        md_data = format_markdown_report(result)
        Path(args.export_md).write_text(md_data, encoding="utf-8")

    print(format_markdown_report(result))
    return 0 if result.status == ConvergenceStatus.CONVERGED else 1


if __name__ == "__main__":
    sys.exit(main())
