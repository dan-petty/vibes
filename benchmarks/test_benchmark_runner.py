"""Unit tests for Multi-Agent Benchmark Suite."""

from benchmark_runner import (
    calculate_cyclomatic_complexity,
    run_all_benchmarks,
    run_cegis_benchmark,
    run_complexity_benchmark,
    run_token_economy_benchmark,
)


def test_calculate_cyclomatic_complexity():
    code = """
def simple_branches(x):
    if x > 0:
        return 1
    elif x < 0:
        return -1
    return 0
"""
    # 1 base + 2 If/elif = 3
    complexity = calculate_cyclomatic_complexity(code)
    assert complexity == 3


def test_complexity_benchmark_track():
    res = run_complexity_benchmark()
    assert res.track_name == "ComplexityRefactoring"
    assert res.passed is True
    assert res.metrics["reduction_percentage"] > 50.0
    assert res.metrics["refactored_complexity"] <= 10


def test_cegis_benchmark_track():
    res = run_cegis_benchmark()
    assert res.track_name == "CEGISConvergence"
    assert res.passed is True
    assert res.metrics["converged"] is True
    assert res.metrics["rounds_to_convergence"] <= 6


def test_token_economy_benchmark_track():
    res = run_token_economy_benchmark()
    assert res.track_name == "TokenEconomy"
    assert res.passed is True
    assert res.metrics["cloud_token_savings_pct"] >= 70.0


def test_run_all_benchmarks_scorecard():
    scorecard = run_all_benchmarks()
    assert scorecard.total_tracks == 3
    assert scorecard.passed_tracks == 3
    assert scorecard.overall_score >= 70.0
    assert len(scorecard.results) == 3
