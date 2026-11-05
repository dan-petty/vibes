"""Unit tests for Multi-Agent Benchmark Suite."""

from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

_benchmarks_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(_benchmarks_dir))

import benchmark_runner
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


@pytest.fixture
def _fresh_cargo_cache() -> Iterator[None]:
    """Clear the cached cargo invocation so each test observes only its own stub.

    ``_run_cargo_test_suite`` is ``functools.cache``d, so a result recorded by an
    earlier call would otherwise answer for a call whose subprocess never ran.
    """
    benchmark_runner._run_cargo_test_suite.cache_clear()
    yield
    benchmark_runner._run_cargo_test_suite.cache_clear()


def _make_crate_dir(tmp_path: Path) -> Path:
    """Create a crate directory whose Cargo.toml satisfies the presence check."""
    crate_dir = tmp_path / "crate"
    crate_dir.mkdir()
    (crate_dir / "Cargo.toml").write_text('[package]\nname = "probe"\n', encoding="utf-8")
    return crate_dir


@pytest.mark.usefixtures("_fresh_cargo_cache")
def test_cargo_missing_returns_failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A machine without cargo reports a failed track, not four phantom tests."""
    crate_dir = _make_crate_dir(tmp_path)
    monkeypatch.setattr(benchmark_runner.shutil, "which", lambda _name: None)

    assert benchmark_runner._run_cargo_test_suite(crate_dir) == (False, 0)


@pytest.mark.usefixtures("_fresh_cargo_cache")
def test_cargo_timeout_returns_failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A cargo run killed by the timeout reports a failed track with zero tests."""
    crate_dir = _make_crate_dir(tmp_path)
    monkeypatch.setattr(benchmark_runner.shutil, "which", lambda _name: "/usr/local/bin/cargo")

    def _raise_timeout(*_args: object, **_kwargs: object) -> None:
        raise subprocess.TimeoutExpired(cmd="cargo test", timeout=45)

    monkeypatch.setattr(benchmark_runner.subprocess, "run", _raise_timeout)

    assert benchmark_runner._run_cargo_test_suite(crate_dir) == (False, 0)


@pytest.mark.usefixtures("_fresh_cargo_cache")
def test_cargo_oserror_returns_failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A cargo executable that cannot be executed reports a failed track, not a pass."""
    crate_dir = _make_crate_dir(tmp_path)
    monkeypatch.setattr(benchmark_runner.shutil, "which", lambda _name: "/usr/local/bin/cargo")

    def _raise_oserror(*_args: object, **_kwargs: object) -> None:
        raise OSError("cargo vanished between lookup and exec")

    monkeypatch.setattr(benchmark_runner.subprocess, "run", _raise_oserror)

    assert benchmark_runner._run_cargo_test_suite(crate_dir) == (False, 0)


def test_run_all_benchmarks_scorecard() -> None:
    """Verify aggregate scorecard compilation across all four evaluation tracks.

    The Rust track can only pass where cargo actually ran, so the count of passed
    tracks follows the toolchain rather than assuming one is installed: a scorecard
    that always claims four passes is the false pass this suite exists to catch.
    """
    scorecard = run_all_benchmarks()
    rust_ran = shutil.which("cargo") is not None

    assert (scorecard.total_tracks, scorecard.passed_tracks, len(scorecard.results)) == (
        4,
        4 if rust_ran else 3,
        4,
    )
    # The aggregate is the mean of the track scores, which is the claim the scorecard makes.
    assert scorecard.overall_score == round(sum(res.score for res in scorecard.results) / 4, 1)
    # The >= 70 threshold is a full-toolchain claim: without cargo the Rust track scores 0.
    if rust_ran:
        assert scorecard.overall_score >= 70.0
