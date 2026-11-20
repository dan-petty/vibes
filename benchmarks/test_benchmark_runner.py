"""Unit tests for Multi-Agent Benchmark Suite."""

from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import NoReturn

import pytest

_benchmarks_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(_benchmarks_dir))

import benchmark_runner
from benchmark_runner import (
    _run_cargo_test_suite,
    calculate_cyclomatic_complexity,
    run_all_benchmarks,
    run_cegis_benchmark,
    run_complexity_benchmark,
    run_rust_typestate_benchmark,
    run_token_economy_benchmark,
)

# The Rust track fails when cargo cannot run, so its tests skip, with a reason, where
# cargo is not installed. GitHub's runners have cargo, so CI runs them.
requires_cargo = pytest.mark.skipif(
    shutil.which("cargo") is None,
    reason="cargo is not on PATH, so the Rust track cannot run cargo test",
)

_CARGO_TOML = '[package]\nname = "stand_in"\nversion = "0.1.0"\n'


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


@requires_cargo
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


@requires_cargo
def test_run_all_benchmarks_scorecard() -> None:
    """Verify aggregate scorecard compilation across all four evaluation tracks."""
    scorecard = run_all_benchmarks()
    assert (
        scorecard.total_tracks,
        scorecard.passed_tracks,
        len(scorecard.results),
        scorecard.overall_score >= 70.0,
    ) == (4, 4, 4, True)


@pytest.fixture
def fresh_cargo_cache() -> Iterator[None]:
    """Clear the cached cargo outcome so that a test sees its own PATH."""
    _run_cargo_test_suite.cache_clear()
    yield
    _run_cargo_test_suite.cache_clear()


def _put_stand_in_cargo_on_path(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    """Make shutil.which find a cargo file. The tests replace subprocess.run, so it never runs."""
    bin_dir = root / "bin"
    bin_dir.mkdir()
    cargo = bin_dir / "cargo"
    cargo.write_text("", encoding="utf-8")
    cargo.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))


def test_cargo_suite_fails_when_cargo_is_not_on_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Without cargo the suite did not run, so it must not count as 4 passed tests."""
    (tmp_path / "Cargo.toml").write_text(_CARGO_TOML, encoding="utf-8")
    monkeypatch.setenv("PATH", str(tmp_path))
    assert _run_cargo_test_suite(tmp_path) == (False, 0, "cargo is not on PATH")


def test_cargo_suite_fails_without_a_cargo_manifest(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A crate directory with no Cargo.toml has no tests to run."""
    _put_stand_in_cargo_on_path(monkeypatch, tmp_path)
    assert _run_cargo_test_suite(tmp_path) == (False, 0, "no Cargo.toml in the crate directory")


@pytest.mark.parametrize(
    ("error", "detail"),
    [
        (subprocess.TimeoutExpired(cmd="cargo test", timeout=45), "cargo test timed out after 45 s"),
        (OSError("stand-in failure"), "cargo test could not start: stand-in failure"),
    ],
    ids=["timeout", "oserror"],
)
def test_cargo_suite_fails_when_cargo_test_does_not_finish(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, error: Exception, detail: str
) -> None:
    """A cargo run that times out or cannot start fails the track and counts no tests."""
    _put_stand_in_cargo_on_path(monkeypatch, tmp_path)
    (tmp_path / "Cargo.toml").write_text(_CARGO_TOML, encoding="utf-8")

    def raise_error(*_args: object, **_kwargs: object) -> NoReturn:
        raise error

    monkeypatch.setattr(benchmark_runner.subprocess, "run", raise_error)
    assert _run_cargo_test_suite(tmp_path) == (False, 0, detail)


def test_cargo_suite_fails_when_cargo_test_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A non-zero exit of cargo test fails the track and names the exit code."""
    _put_stand_in_cargo_on_path(monkeypatch, tmp_path)
    (tmp_path / "Cargo.toml").write_text(_CARGO_TOML, encoding="utf-8")
    monkeypatch.setattr(
        benchmark_runner.subprocess,
        "run",
        lambda args, **_kwargs: subprocess.CompletedProcess(args, 101, "", ""),
    )
    assert _run_cargo_test_suite(tmp_path) == (False, 0, "cargo test exited with code 101")


@pytest.mark.usefixtures("fresh_cargo_cache")
def test_rust_track_fails_with_zero_tests_when_cargo_is_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The track says why it is not verified instead of reporting tests that never ran."""
    monkeypatch.setenv("PATH", str(tmp_path))
    res = run_rust_typestate_benchmark()
    assert (res.passed, res.score, res.metrics["cargo_tests_passed"], res.summary) == (
        False,
        0.0,
        0,
        "Rust track not verified: cargo is not on PATH.",
    )
