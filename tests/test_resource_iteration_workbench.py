"""Unit tests for the Resource Iteration Workbench."""

import json
from pathlib import Path
import sys
import pytest

# Add tools/ to sys.path for direct imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

from resource_iteration_workbench import (
    ASTMetricCalculator,
    FeedbackAnalyzer,
    FeedbackCategory,
    FeedbackPriority,
    HealthStatus,
    ImprovementFeedback,
    OutputReviewer,
    ResourceIterationWorkbench,
    ResourceRunner,
    ResourceScanMetrics,
    ResourceScanner,
    ResourceType,
    ReviewEvaluation,
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
    assert "improvement_feedback" in data


def test_feedback_analyzer_refactoring_headroom() -> None:
    """Ensure FeedbackAnalyzer detects near-threshold complexity and nesting."""
    scan = ResourceScanMetrics(
        file_path="tools/analyzer.py",
        resource_type=ResourceType.PYTHON_MODULE,
        max_complexity=8,
        max_depth=4,
        near_threshold_functions=["route_packet (M=8, Depth=4)"],
    )
    eval_item = ReviewEvaluation(
        resource_path="tools/analyzer.py",
        health_status=HealthStatus.HEALTHY,
        quality_score=100.0,
        scan_metrics=scan,
    )
    feedback = FeedbackAnalyzer.generate_feedback([eval_item], Path("."))
    refactors = [f for f in feedback if f.category == FeedbackCategory.PROACTIVE_REFACTOR]
    assert len(refactors) == 1
    assert refactors[0].priority == FeedbackPriority.MEDIUM
    assert "route_packet" in refactors[0].prescriptive_guidance


def test_feedback_analyzer_documentation_and_typing() -> None:
    """Ensure FeedbackAnalyzer flags public functions missing docstrings or type hints."""
    scan = ResourceScanMetrics(
        file_path="tools/service.py",
        resource_type=ResourceType.PYTHON_MODULE,
        functions_without_docstrings=["start_engine"],
        functions_without_type_hints=["start_engine"],
    )
    eval_item = ReviewEvaluation(
        resource_path="tools/service.py",
        health_status=HealthStatus.HEALTHY,
        quality_score=100.0,
        scan_metrics=scan,
    )
    feedback = FeedbackAnalyzer.generate_feedback([eval_item], Path("."))
    docs = [f for f in feedback if f.category == FeedbackCategory.DOCUMENTATION]
    types = [f for f in feedback if f.category == FeedbackCategory.TYPE_SAFETY]
    assert len(docs) == 1
    assert len(types) == 1
    assert "start_engine" in docs[0].prescriptive_guidance
    assert "start_engine" in types[0].prescriptive_guidance


def test_feedback_analyzer_test_parity() -> None:
    """Ensure FeedbackAnalyzer detects missing companion test suites."""
    mod_scan = ResourceScanMetrics(
        file_path="tools/orphan_module.py",
        resource_type=ResourceType.PYTHON_MODULE,
    )
    eval_item = ReviewEvaluation(
        resource_path="tools/orphan_module.py",
        health_status=HealthStatus.HEALTHY,
        quality_score=100.0,
        scan_metrics=mod_scan,
    )
    feedback = FeedbackAnalyzer.generate_feedback([eval_item], Path("."))
    parity = [f for f in feedback if f.category == FeedbackCategory.TEST_PARITY]
    assert len(parity) == 1
    assert parity[0].priority == FeedbackPriority.HIGH
    assert "orphan_module" in parity[0].headline


def test_feedback_analyzer_positive_reinforcement() -> None:
    """Ensure clean, elegant modules receive positive reinforcement certifications."""
    mod_scan = ResourceScanMetrics(
        file_path="tools/elegant.py",
        resource_type=ResourceType.PYTHON_MODULE,
        max_complexity=3,
        max_depth=2,
    )
    eval_item = ReviewEvaluation(
        resource_path="tools/elegant.py",
        health_status=HealthStatus.HEALTHY,
        quality_score=100.0,
        scan_metrics=mod_scan,
    )
    feedback = FeedbackAnalyzer.generate_feedback([eval_item], Path("."))
    reinforce = [f for f in feedback if f.category == FeedbackCategory.POSITIVE_REINFORCEMENT]
    assert len(reinforce) == 1
    assert "elegant" in reinforce[0].headline


def test_export_feedback_to_sdlc_tasks() -> None:
    """Ensure feedback items are mapped to structured SDLC backlog tasks."""
    feedback_items = [
        ImprovementFeedback(
            category=FeedbackCategory.PROACTIVE_REFACTOR,
            priority=FeedbackPriority.MEDIUM,
            target="tools/parser.py",
            headline="Decompose complex parser",
            prescriptive_guidance="M=9 approaching cap",
            suggested_action="Refactor into helpers",
        ),
        ImprovementFeedback(
            category=FeedbackCategory.POSITIVE_REINFORCEMENT,
            priority=FeedbackPriority.INFO,
            target="tools/clean.py",
            headline="Elegance achieved",
            prescriptive_guidance="Great design",
            suggested_action="Preserve pattern",
        ),
    ]
    tasks = ResourceIterationWorkbench.export_feedback_to_sdlc(feedback_items)
    assert len(tasks) == 1
    assert tasks[0]["priority"] == "P2_MEDIUM"
    assert tasks[0]["kind"] == "issue"
    assert "PROACTIVE_REFACTOR" in tasks[0]["title"]


def test_cli_export_backlog_flag(tmp_path: Path) -> None:
    """Ensure --export-backlog writes a valid backlog file via CLI."""
    (tmp_path / "module.py").write_text("def run(): pass\n", encoding="utf-8")
    backlog_path = tmp_path / "backlog.json"

    exit_code = main(["--root", str(tmp_path), "--skip-tests", "--export-backlog", str(backlog_path)])
    assert exit_code == 0
    assert backlog_path.is_file()
    data = json.loads(backlog_path.read_text(encoding="utf-8"))
    assert isinstance(data, list)
