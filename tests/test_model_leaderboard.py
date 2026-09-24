"""Tests for Live Multi-Model Leaderboard & Cost-Per-Invariant Index.

Verifies model pricing parsing, AST invariant evaluation, patch minimality scoring,
token cost calculations, leaderboard ranking, markdown/json reporting, and CLI interfaces.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

from model_leaderboard import (
    DEFAULT_PRICING,
    ModelPricing,
    ModelRun,
    ScoredRun,
    aggregate_model_runs,
    build_leaderboard,
    calculate_token_cost,
    compute_patch_minimality,
    evaluate_code_ast,
    format_json_report,
    format_markdown_report,
    generate_sample_benchmark_runs,
    main,
    parse_pricing_catalog,
    score_run,
)


def test_parse_pricing_catalog_default() -> None:
    """Verify parsing of the default pricing catalog."""
    catalog = parse_pricing_catalog(DEFAULT_PRICING)
    claude = catalog["claude-3-5-sonnet"]
    deepseek = catalog["deepseek-v3"]
    local_qwen = catalog["qwen-2.5-coder-7b-local"]

    assert (
        claude.provider,
        claude.input_usd_per_m,
        claude.output_usd_per_m,
        claude.is_local,
    ) == ("Anthropic", 3.00, 15.00, False)

    assert (
        deepseek.provider,
        deepseek.input_usd_per_m,
        deepseek.output_usd_per_m,
        deepseek.is_local,
    ) == ("DeepSeek", 0.27, 1.10, False)

    assert (
        local_qwen.provider,
        local_qwen.input_usd_per_m,
        local_qwen.output_usd_per_m,
        local_qwen.is_local,
    ) == ("Self-Hosted", 0.00, 0.00, True)


def test_calculate_token_cost_cloud_and_local() -> None:
    """Verify token cost calculation for paid cloud APIs and zero-cost local models."""
    cloud_pricing = ModelPricing(
        model_id="mock-cloud",
        display_name="Mock Cloud",
        provider="MockProvider",
        input_usd_per_m=2.00,
        output_usd_per_m=10.00,
        is_local=False,
    )
    local_pricing = ModelPricing(
        model_id="mock-local",
        display_name="Mock Local",
        provider="Local",
        input_usd_per_m=0.00,
        output_usd_per_m=0.00,
        is_local=True,
    )

    cost_cloud = calculate_token_cost(10_000, 2_000, cloud_pricing)
    cost_local = calculate_token_cost(50_000, 10_000, local_pricing)

    expected_cloud = (10_000 * 2.0 / 1_000_000.0) + (2_000 * 10.0 / 1_000_000.0)
    assert (round(cost_cloud, 6), cost_local) == (round(expected_cloud, 6), 0.0)


def test_evaluate_code_ast_empty_and_invalid() -> None:
    """Verify AST metrics computation on empty strings and syntax error code."""
    empty_metrics = evaluate_code_ast("   ")
    invalid_metrics = evaluate_code_ast("def broken_syntax(:")

    assert (
        empty_metrics.max_complexity,
        empty_metrics.max_depth,
        empty_metrics.docstring_coverage,
        empty_metrics.type_coverage,
        empty_metrics.function_count,
    ) == (1, 1, 1.0, 1.0, 0)

    assert (
        invalid_metrics.max_complexity,
        invalid_metrics.max_depth,
        invalid_metrics.docstring_coverage,
        invalid_metrics.type_coverage,
    ) == (15, 6, 0.0, 0.0)


def test_evaluate_code_ast_clean_and_complex() -> None:
    """Verify AST metrics computation on clean functions and nested branch structures."""
    clean_code = 'def add(a: int, b: int) -> int:\n    """Sum two numbers."""\n    return a + b\n'
    nested_code = (
        "def nested_logic(vals):\n"
        "    for x in vals:\n"
        "        if x > 0:\n"
        "            while x < 10:\n"
        "                x += 1\n"
        "    return vals\n"
    )

    clean_metrics = evaluate_code_ast(clean_code)
    nested_metrics = evaluate_code_ast(nested_code)

    assert (
        clean_metrics.max_complexity,
        clean_metrics.max_depth,
        clean_metrics.docstring_coverage,
        clean_metrics.type_coverage,
        clean_metrics.function_count,
    ) == (1, 2, 1.0, 1.0, 1)

    assert (
        nested_metrics.max_complexity >= 4,
        nested_metrics.max_depth >= 3,
        nested_metrics.docstring_coverage,
        nested_metrics.type_coverage,
    ) == (True, True, 0.0, 0.0)


def test_compute_patch_minimality_identical_and_surgical() -> None:
    """Verify patch minimality for identical files and surgical 1-line edits."""
    base_code = "def compute(a: int) -> int:\n    return a * 2\n"
    surgical_code = "def compute(a: int) -> int:\n    return a * 3\n"

    identical_score = compute_patch_minimality(base_code, base_code)
    surgical_score = compute_patch_minimality(base_code, surgical_code)

    assert (identical_score, surgical_score >= 0.70) == (1.0, True)


def test_compute_patch_minimality_sprawling_noise() -> None:
    """Verify patch minimality penalizes excessive noise lines and comments."""
    base_code = "def check(x):\n    return x\n"
    noisy_code = (
        "# Line 1 comment\n"
        "# Line 2 comment\n"
        "# Line 3 comment\n"
        "def check(x):\n"
        "    # Inner comment\n"
        "    return x\n"
    )
    score = compute_patch_minimality(base_code, noisy_code)
    assert score < 0.60


def test_score_run_invariants_and_headroom() -> None:
    """Verify evaluation and invariant satisfaction scoring for a model run."""
    pricing_catalog = parse_pricing_catalog(DEFAULT_PRICING)
    clean_code = 'def add(a: int, b: int) -> int:\n    """Sum two numbers."""\n    return a + b\n'
    run = ModelRun(
        task_id="task-01",
        model_id="claude-3-5-sonnet",
        tests_passed=True,
        prompt_tokens=1000,
        completion_tokens=200,
        execution_seconds=1.5,
        code_before=clean_code,
        code_after=clean_code,
    )
    scored = score_run(run, pricing_catalog)

    assert (
        scored.task_id,
        scored.model_id,
        scored.tests_passed,
        scored.invariants_satisfied,
        scored.headroom_pass,
        scored.cost_usd > 0.0,
    ) == ("task-01", "claude-3-5-sonnet", True, 6, True, True)


def test_aggregate_model_runs_empty_and_populated() -> None:
    """Verify aggregation across empty and multiple scored runs."""
    pricing = ModelPricing("test-model", "Test Model", "TestProvider", 1.0, 2.0, False)
    empty_entry = aggregate_model_runs("test-model", [], pricing)

    assert (
        empty_entry.tasks_attempted,
        empty_entry.tasks_passed,
        empty_entry.pass_at_1,
        empty_entry.composite_score,
    ) == (0, 0, 0.0, 0.0)

    runs = [
        ScoredRun("t1", "test-model", True, 1, 1, 1.0, 1.0, 0.9, 6, 0.001, 1.0, True),
        ScoredRun("t2", "test-model", False, 4, 2, 1.0, 1.0, 0.8, 5, 0.001, 1.0, True),
    ]
    entry = aggregate_model_runs("test-model", runs, pricing)

    assert (
        entry.tasks_attempted,
        entry.tasks_passed,
        entry.pass_at_1,
        entry.total_cost_usd,
        entry.composite_score > 50.0,
    ) == (2, 1, 50.0, 0.002, True)


def test_build_leaderboard_ranking() -> None:
    """Verify leaderboard construction and ranking order."""
    runs = generate_sample_benchmark_runs()
    report = build_leaderboard(runs)

    assert (
        report.total_runs,
        report.models_evaluated,
        len(report.entries),
        report.entries[0].rank,
    ) == (5, 5, 5, 1)

    ranks = [e.rank for e in report.entries]
    assert ranks == [1, 2, 3, 4, 5]


def test_format_markdown_and_json_reports() -> None:
    """Verify markdown and json serialization formatting."""
    runs = generate_sample_benchmark_runs()
    report = build_leaderboard(runs)

    md_text = format_markdown_report(report)
    json_text = format_json_report(report)

    assert (
        "# 🏆 Multi-Model Benchmark Leaderboard" in md_text,
        "quadrantChart" in md_text,
        "```mermaid" in md_text,
    ) == (True, True, True)

    parsed = json.loads(json_text)
    assert (
        parsed["total_runs"],
        parsed["models_evaluated"],
        len(parsed["entries"]),
    ) == (5, 5, 5)


def test_cli_sample_execution(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    """Verify CLI execution of sample benchmark with stdout and file outputs."""
    out_file = tmp_path / "report.md"
    exit_code_stdout = main(["sample"])
    captured = capsys.readouterr()

    exit_code_file = main(["sample", "--format", "json", "--output", str(out_file)])

    assert (
        exit_code_stdout,
        exit_code_file,
        "# 🏆 Multi-Model Benchmark Leaderboard" in captured.out,
        out_file.exists(),
    ) == (0, 0, True, True)


def test_cli_minimality_execution(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    """Verify CLI minimality subcommand compares two files."""
    file_a = tmp_path / "a.py"
    file_b = tmp_path / "b.py"
    file_a.write_text("def fn(): return 1\n", encoding="utf-8")
    file_b.write_text("def fn(): return 2\n", encoding="utf-8")

    exit_code = main(["minimality", str(file_a), str(file_b)])
    captured = capsys.readouterr()

    assert (exit_code, "Patch Minimality:" in captured.out) == (0, True)


def test_cli_score_execution(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    """Verify CLI score subcommand ingests a JSON run log."""
    json_path = tmp_path / "runs.json"
    run_payload = {
        "runs": [
            {
                "task_id": "test-task",
                "model_id": "claude-3-5-sonnet",
                "tests_passed": True,
                "prompt_tokens": 1500,
                "completion_tokens": 300,
                "execution_seconds": 1.2,
                "code_before": "def fn(): return 1\n",
                "code_after": 'def fn() -> int:\n    """Return one."""\n    return 1\n',
            }
        ]
    }
    json_path.write_text(json.dumps(run_payload), encoding="utf-8")

    exit_code = main(["score", str(json_path)])
    captured = capsys.readouterr()

    assert (exit_code, "Claude 3.5 Sonnet" in captured.out) == (0, True)


def test_cli_error_handling(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    """Verify CLI error handling for missing files and malformed JSON."""
    missing = tmp_path / "nonexistent.json"
    broken = tmp_path / "broken.json"
    broken.write_text("{broken json", encoding="utf-8")

    code_missing = main(["score", str(missing)])
    code_broken = main(["score", str(broken)])
    code_min_missing = main(["minimality", str(missing), str(broken)])

    assert (code_missing, code_broken, code_min_missing) == (1, 1, 1)
