"""Unit tests for the Resource Iteration Workbench."""

import json
from pathlib import Path
import sys
import pytest

# Add tools/ to sys.path for direct imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

from resource_iteration_workbench import (
    ASTMetricCalculator,
    HealthStatus,
    OutputReviewer,
    ResourceIterationWorkbench,
    ResourceRunner,
    ResourceScanMetrics,
    ResourceScanner,
    ResourceType,
    RunExecutionResult,
    main,
)


def test_resource_scanner_analyzes_metrics(tmp_path: Path) -> None:
    """Ensure ResourceScanner accurately computes LOC, complexity, and nesting depth."""
    sample_code = """
def sample_func(x: int) -> int:
    if x > 10:
        if x > 20:
            return x * 2
        return x + 5
    return 0

class MyClass:
    def method(self) -> None:
        pass
"""
    py_file = tmp_path / "module.py"
    py_file.write_text(sample_code, encoding="utf-8")

    metrics = ResourceScanner.scan_python_file(py_file)
    assert metrics.loc > 0
    assert metrics.functions_count == 2  # sample_func + method
    assert metrics.classes_count == 1
    assert metrics.max_complexity == 3  # sample_func has 2 ifs
    assert metrics.max_depth == 3
    assert len(metrics.complexity_violations) == 0


def test_ast_metric_calculator_sanitization(tmp_path: Path) -> None:
    """Ensure ASTMetricCalculator detects private RFC 1918 IPs and non-standard subdomains."""
    dirty_code = """
URL_A = "http://10.0.0.5/api"
URL_B = "https://sub.example.com/status"
"""
    py_file = tmp_path / "dirty.py"
    py_file.write_text(dirty_code, encoding="utf-8")

    metrics = ResourceScanner.scan_python_file(py_file)
    assert len(metrics.sanitization_violations) == 2
    assert any("10.0.0.5" in v for v in metrics.sanitization_violations)
    assert any("sub.example.com" in v for v in metrics.sanitization_violations)


def test_output_reviewer_evaluates_healthy_resource() -> None:
    """Ensure a clean resource with passed tests receives a 100 score and HEALTHY status."""
    scan = ResourceScanMetrics(
        file_path="clean.py",
        resource_type=ResourceType.PYTHON_MODULE,
        max_complexity=4,
        max_depth=2,
    )
    run = RunExecutionResult(
        target="clean.py",
        command=["pytest"],
        exit_code=0,
        duration_seconds=0.5,
        stdout="5 passed in 0.5s",
        stderr="",
        passed_count=5,
        failed_count=0,
    )

    evaluation = OutputReviewer.evaluate(scan, run)
    assert evaluation.quality_score == 100.0
    assert evaluation.health_status == HealthStatus.HEALTHY
    assert len(evaluation.actionable_recommendations) == 0


def test_output_reviewer_deducts_for_violations_and_failures() -> None:
    """Ensure score deductions occur for complexity breaches and test failures."""
    scan = ResourceScanMetrics(
        file_path="problematic.py",
        resource_type=ResourceType.PYTHON_MODULE,
        max_complexity=12,
        complexity_violations=["func:10 M=12 > 10"],
    )
    run = RunExecutionResult(
        target="problematic.py",
        command=["pytest"],
        exit_code=1,
        duration_seconds=1.2,
        stdout="2 failed, 3 passed",
        stderr="",
        passed_count=3,
        failed_count=2,
        warnings_count=1,
    )

    evaluation = OutputReviewer.evaluate(scan, run)
    assert evaluation.quality_score < 50.0
    assert evaluation.health_status == HealthStatus.CRITICAL
    assert len(evaluation.actionable_recommendations) >= 2


def test_resource_runner_parses_pytest_counts() -> None:
    """Ensure ResourceRunner regex extracts test counts from raw stdout."""
    stdout_sample = "===== 14 passed, 2 failed, 3 warnings in 2.15s ====="
    passed, failed, warnings = ResourceRunner._parse_pytest_counts(stdout_sample)
    assert passed == 14
    assert failed == 2
    assert warnings == 3


def test_workbench_full_cycle_skip_tests(tmp_path: Path) -> None:
    """Ensure ResourceIterationWorkbench runs an end-to-end cycle and writes baseline."""
    code_dir = tmp_path / "src"
    code_dir.mkdir()
    (code_dir / "app.py").write_text("def hello(): return 'world'\n", encoding="utf-8")

    baseline_file = tmp_path / "baseline.json"
    workbench = ResourceIterationWorkbench(root_dir=tmp_path, baseline_path=baseline_file)
    report = workbench.run_cycle(execute_tests=False)

    assert report.overall_health == HealthStatus.HEALTHY
    assert report.overall_score == 100.0
    assert len(report.resources_evaluated) >= 1
    assert baseline_file.is_file()

    # Re-running with existing baseline computes deltas
    report2 = workbench.run_cycle(execute_tests=False)
    eval_entry = report2.resources_evaluated[0]
    assert "complexity_delta" in eval_entry.metric_deltas


def test_cli_main_entrypoint(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Ensure main CLI correctly executes with --json flag."""
    (tmp_path / "test_dummy.py").write_text("def test_ok(): assert True\n", encoding="utf-8")

    exit_code = main(["--root", str(tmp_path), "--skip-tests", "--json"])
    assert exit_code == 0

    captured = capsys.readouterr().out
    data = json.loads(captured)
    assert "overall_health" in data
    assert "overall_score" in data
    assert "resources_evaluated" in data
