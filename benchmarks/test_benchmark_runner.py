"""Unit tests for Multi-Agent Benchmark Suite."""

from __future__ import annotations

import sys
from pathlib import Path

_benchmarks_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(_benchmarks_dir))

from benchmark_runner import (
    calculate_cyclomatic_complexity,
    run_all_benchmarks,
    run_cegis_benchmark,
    run_complexity_benchmark,
    run_rust_typestate_benchmark,
    run_token_economy_benchmark,
)


def test_calculate_cyclomatic_complexity() -> None:
    """Verify cyclomatic complexity calculation on branch statement AST nodes."""
    code = """
def simple_branches(x):
    if x > 0:
        return 1
    elif x < 0:
        return -1
    return 0
"""
    complexity = calculate_cyclomatic_complexity(code)
    assert complexity == 3


def test_complexity_benchmark_track() -> None:
    """Verify complexity refactoring track satisfies threshold constraints."""
    res = run_complexity_benchmark()
    assert (
        res.track_name,
        res.passed,
        res.metrics["reduction_percentage"] > 50.0,
        res.metrics["refactored_complexity"] <= 10,
    ) == ("ComplexityRefactoring", True, True, True)


def test_cegis_benchmark_track() -> None:
    """Verify CEGIS defect convergence track finishes within bounded rounds."""
    res = run_cegis_benchmark()
    assert (
        res.track_name,
        res.passed,
        res.metrics["converged"],
        res.metrics["rounds_to_convergence"] <= 6,
    ) == ("CEGISConvergence", True, True, True)


def test_token_economy_benchmark_track() -> None:
    """Verify multi-tier token economy offloading delivers >= 70% cloud token savings."""
    res = run_token_economy_benchmark()
    assert (
        res.track_name,
        res.passed,
        res.metrics["cloud_token_savings_pct"] >= 70.0,
    ) == ("TokenEconomy", True, True)


def test_rust_typestate_benchmark_track() -> None:
    """Verify Rust type-state benchmark enforces memory safety and affine state transitions."""
    res = run_rust_typestate_benchmark()
    assert (
        res.track_name,
        res.passed,
        res.score,
        res.metrics["forbid_unsafe_code"],
        res.metrics["unsafe_blocks"],
        res.metrics["memory_overhead_bytes"],
    ) == ("RustTypeState", True, 100.0, True, 0, 0)


def test_run_all_benchmarks_scorecard() -> None:
    """Verify aggregate scorecard compilation across all four evaluation tracks."""
    scorecard = run_all_benchmarks()
    assert (
        scorecard.total_tracks,
        scorecard.passed_tracks,
        len(scorecard.results),
        scorecard.overall_score >= 70.0,
    ) == (4, 4, 4, True)
