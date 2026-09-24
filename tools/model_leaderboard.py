#!/usr/bin/env python3
"""Live Multi-Model Leaderboard & Cost-Per-Invariant Index.

Evaluates and ranks frontier and open-weights models across autonomous engineering
invariants: Pass@1, McCabe cyclomatic complexity (M <= 10), nesting depth (<= 5),
patch minimality, docstring/type completeness, and cloud token expenditure.
"""

from __future__ import annotations

import argparse
import ast
import difflib
import json
import math
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

# Default token pricing catalog (USD per 1M tokens)
# Source: Public API rate sheets (Anthropic, OpenAI, DeepSeek, Alibaba Cloud, Google Cloud)
DEFAULT_PRICING: Final[dict[str, dict[str, Any]]] = {
    "claude-3-5-sonnet": {
        "display_name": "Claude 3.5 Sonnet",
        "provider": "Anthropic",
        "input_usd_per_m": 3.00,
        "output_usd_per_m": 15.00,
        "is_local": False,
    },
    "gpt-4o": {
        "display_name": "GPT-4o",
        "provider": "OpenAI",
        "input_usd_per_m": 2.50,
        "output_usd_per_m": 10.00,
        "is_local": False,
    },
    "deepseek-v3": {
        "display_name": "DeepSeek-V3",
        "provider": "DeepSeek",
        "input_usd_per_m": 0.27,
        "output_usd_per_m": 1.10,
        "is_local": False,
    },
    "deepseek-r1": {
        "display_name": "DeepSeek-R1",
        "provider": "DeepSeek",
        "input_usd_per_m": 0.55,
        "output_usd_per_m": 2.19,
        "is_local": False,
    },
    "qwen-2.5-coder-32b": {
        "display_name": "Qwen-2.5-Coder-32B",
        "provider": "Alibaba",
        "input_usd_per_m": 0.20,
        "output_usd_per_m": 0.60,
        "is_local": False,
    },
    "gemini-1.5-pro": {
        "display_name": "Gemini 1.5 Pro",
        "provider": "Google",
        "input_usd_per_m": 1.25,
        "output_usd_per_m": 5.00,
        "is_local": False,
    },
    "gemini-1.5-flash": {
        "display_name": "Gemini 1.5 Flash",
        "provider": "Google",
        "input_usd_per_m": 0.075,
        "output_usd_per_m": 0.30,
        "is_local": False,
    },
    "qwen-2.5-coder-7b-local": {
        "display_name": "Qwen-2.5-Coder-7B (Local)",
        "provider": "Self-Hosted",
        "input_usd_per_m": 0.00,
        "output_usd_per_m": 0.00,
        "is_local": True,
    },
}

NESTING_NODE_TYPES: Final[tuple[type[ast.AST], ...]] = (
    ast.If,
    ast.For,
    ast.While,
    ast.Try,
    ast.With,
    ast.AsyncWith,
    ast.AsyncFor,
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.ClassDef,
)

DECISION_NODE_TYPES: Final[tuple[type[ast.AST], ...]] = (
    ast.If,
    ast.For,
    ast.While,
    ast.ExceptHandler,
    ast.With,
    ast.Assert,
    ast.BoolOp,
)


@dataclass(frozen=True)
class ModelPricing:
    """Pricing configuration and provider metadata for a language model."""

    model_id: str
    display_name: str
    provider: str
    input_usd_per_m: float
    output_usd_per_m: float
    is_local: bool = False


@dataclass(frozen=True)
class ASTMetrics:
    """Syntax tree analysis metrics for generated Python code."""

    max_complexity: int
    max_depth: int
    docstring_coverage: float
    type_coverage: float
    function_count: int
    node_count: int


@dataclass
class ModelRun:
    """A single benchmark task execution run by a model."""

    task_id: str
    model_id: str
    tests_passed: bool
    prompt_tokens: int
    completion_tokens: int
    execution_seconds: float
    code_before: str = ""
    code_after: str = ""
    diff_text: str = ""


@dataclass(frozen=True)
class ScoredRun:
    """An evaluated benchmark run with calculated invariant metrics and cost."""

    task_id: str
    model_id: str
    tests_passed: bool
    max_complexity: int
    max_depth: int
    docstring_coverage: float
    type_coverage: float
    patch_minimality: float
    invariants_satisfied: int
    cost_usd: float
    execution_seconds: float
    headroom_pass: bool


@dataclass
class ModelLeaderboardEntry:
    """Aggregated evaluation and ranking metrics for a specific model."""

    model_id: str
    display_name: str
    provider: str
    is_local: bool
    tasks_attempted: int
    tasks_passed: int
    pass_at_1: float
    invariant_compliance_rate: float
    headroom_rate: float
    mean_complexity: float
    mean_depth: float
    mean_patch_minimality: float
    total_cost_usd: float
    cost_per_verified_task: float
    cost_per_invariant: float
    composite_score: float
    rank: int = 1


@dataclass
class LeaderboardReport:
    """Complete multi-model benchmark evaluation report."""

    timestamp: str
    total_runs: int
    models_evaluated: int
    entries: list[ModelLeaderboardEntry] = field(default_factory=list)


def parse_pricing_catalog(raw_catalog: dict[str, dict[str, Any]]) -> dict[str, ModelPricing]:
    """Parse a dictionary of raw pricing dictionaries into ModelPricing objects."""
    catalog: dict[str, ModelPricing] = {}
    for model_id, entry in raw_catalog.items():
        catalog[model_id] = ModelPricing(
            model_id=model_id,
            display_name=str(entry.get("display_name", model_id)),
            provider=str(entry.get("provider", "Unknown")),
            input_usd_per_m=float(entry.get("input_usd_per_m", 0.0)),
            output_usd_per_m=float(entry.get("output_usd_per_m", 0.0)),
            is_local=bool(entry.get("is_local", False)),
        )
    return catalog


def calculate_token_cost(prompt_tokens: int, completion_tokens: int, pricing: ModelPricing) -> float:
    """Calculate the total cost in USD for a given token usage."""
    if pricing.is_local:
        return 0.0
    in_cost = (max(0, prompt_tokens) * pricing.input_usd_per_m) / 1_000_000.0
    out_cost = (max(0, completion_tokens) * pricing.output_usd_per_m) / 1_000_000.0
    return in_cost + out_cost


def _calculate_mccabe(tree: ast.AST) -> int:
    """Calculate McCabe cyclomatic complexity of an AST node."""
    decision_points = 1
    for node in ast.walk(tree):
        if isinstance(node, DECISION_NODE_TYPES):
            decision_points += 1
    return decision_points


def _walk_depth(node: ast.AST, depth: int) -> int:
    """Recursively calculate the maximum nesting depth under an AST node."""
    next_depth = depth + 1 if isinstance(node, NESTING_NODE_TYPES) else depth
    max_d = next_depth
    for child in ast.iter_child_nodes(node):
        max_d = max(max_d, _walk_depth(child, next_depth))
    return max_d


def _calculate_max_depth(tree: ast.AST) -> int:
    """Calculate maximum block nesting depth in an AST."""
    statements = [n for n in ast.iter_child_nodes(tree) if isinstance(n, ast.stmt)]
    if not statements:
        return 1
    return max(_walk_depth(stmt, 1) for stmt in statements)


def _measure_docstring_coverage(functions: Sequence[ast.FunctionDef | ast.AsyncFunctionDef]) -> float:
    """Calculate percentage of functions having a docstring."""
    if not functions:
        return 1.0
    documented = sum(1 for fn in functions if ast.get_docstring(fn) is not None)
    return documented / len(functions)


def _has_type_annotations(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Check if a function definition has argument and return type annotations."""
    has_returns = fn.returns is not None
    args = [a for a in fn.args.args if a.arg != "self" and a.arg != "cls"]
    if not args:
        return has_returns
    annotated_args = sum(1 for a in args if a.annotation is not None)
    return has_returns and (annotated_args == len(args))


def _measure_type_coverage(functions: Sequence[ast.FunctionDef | ast.AsyncFunctionDef]) -> float:
    """Calculate percentage of functions with complete type annotations."""
    if not functions:
        return 1.0
    typed = sum(1 for fn in functions if _has_type_annotations(fn))
    return typed / len(functions)


def evaluate_code_ast(code: str) -> ASTMetrics:
    """Parse and calculate AST metrics for a code snippet."""
    if not code.strip():
        return ASTMetrics(1, 1, 1.0, 1.0, 0, 0)
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return ASTMetrics(15, 6, 0.0, 0.0, 0, 0)

    functions = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    nodes = list(ast.walk(tree))
    max_m = max((_calculate_mccabe(fn) for fn in functions), default=_calculate_mccabe(tree))
    max_d = _calculate_max_depth(tree)
    doc_cov = _measure_docstring_coverage(functions)
    type_cov = _measure_type_coverage(functions)
    return ASTMetrics(max_m, max_d, doc_cov, type_cov, len(functions), len(nodes))


def _is_noise_line(line: str) -> bool:
    """Determine whether a diff line represents non-semantic noise."""
    content = line[1:].strip() if len(line) > 1 else ""
    if not content:
        return True
    return content.startswith("#")


def _extract_diff_lines(code_before: str, code_after: str, diff_text: str) -> list[str]:
    """Extract raw diff addition and deletion lines."""
    if diff_text.strip():
        return [line for line in diff_text.splitlines() if line.startswith(("+", "-"))]
    diff_iter = difflib.unified_diff(code_before.splitlines(), code_after.splitlines(), lineterm="")
    return [line for line in diff_iter if line.startswith(("+", "-"))]


def _filter_content_lines(lines: list[str]) -> list[str]:
    """Exclude diff headers from candidate lines."""
    return [line for line in lines if not line.startswith(("---", "+++"))]


def _calculate_minimality_score(content_lines: list[str], before_lines_count: int) -> float:
    """Compute normalized patch minimality ratio from diff content lines."""
    if not content_lines:
        return 1.0
    noise_count = sum(1 for line in content_lines if _is_noise_line(line))
    churn_count = len(content_lines)
    semantic_ratio = max(0.0, (churn_count - noise_count) / churn_count)
    before_lines = max(1, before_lines_count)
    churn_ratio = min(1.0, churn_count / (before_lines * 2.0))
    conciseness = 1.0 - (churn_ratio * 0.5)
    return round(max(0.0, min(1.0, semantic_ratio * conciseness)), 4)


def compute_patch_minimality(code_before: str, code_after: str, diff_text: str = "") -> float:
    """Calculate Patch Minimality Score in [0.0, 1.0] penalizing unnecessary churn."""
    if code_before.strip() == code_after.strip():
        return 1.0
    raw_lines = _extract_diff_lines(code_before, code_after, diff_text)
    content_lines = _filter_content_lines(raw_lines)
    return _calculate_minimality_score(content_lines, len(code_before.splitlines()))


def _count_invariants(metrics: ASTMetrics, passed: bool, minimality: float) -> int:
    """Count number of satisfied autonomous software engineering invariants."""
    checks = (
        passed,
        metrics.max_complexity <= 10,
        metrics.max_depth <= 5,
        metrics.docstring_coverage >= 1.0,
        metrics.type_coverage >= 1.0,
        minimality >= 0.70,
    )
    return sum(1 for c in checks if c)


def score_run(run: ModelRun, pricing_catalog: dict[str, ModelPricing]) -> ScoredRun:
    """Score a single task run against AST invariants, minimality, and token cost."""
    pricing = pricing_catalog.get(
        run.model_id,
        ModelPricing(run.model_id, run.model_id, "Custom", 1.0, 3.0, False),
    )
    cost = calculate_token_cost(run.prompt_tokens, run.completion_tokens, pricing)
    metrics = evaluate_code_ast(run.code_after)
    minimality = compute_patch_minimality(run.code_before, run.code_after, run.diff_text)
    invariants = _count_invariants(metrics, run.tests_passed, minimality)
    headroom = metrics.max_complexity <= 6 and metrics.max_depth <= 2

    return ScoredRun(
        task_id=run.task_id,
        model_id=run.model_id,
        tests_passed=run.tests_passed,
        max_complexity=metrics.max_complexity,
        max_depth=metrics.max_depth,
        docstring_coverage=metrics.docstring_coverage,
        type_coverage=metrics.type_coverage,
        patch_minimality=minimality,
        invariants_satisfied=invariants,
        cost_usd=round(cost, 6),
        execution_seconds=round(run.execution_seconds, 3),
        headroom_pass=headroom,
    )


def _compute_cost_efficiency_score(cost_per_inv: float) -> float:
    """Compute normalized 0-100 cost efficiency rating."""
    if cost_per_inv <= 0.0:
        return 100.0
    return max(0.0, 100.0 - (math.log10(max(1.0, cost_per_inv * 100_000.0)) * 20.0))


def _calculate_composite(pass_rate: float, inv_rate: float, min_mean: float, cpi: float) -> float:
    """Calculate composite score weighting pass rate, invariants, minimality, and cost."""
    pass_weight = pass_rate * 0.35
    inv_weight = inv_rate * 0.30
    min_weight = (min_mean * 100.0) * 0.20
    cost_weight = _compute_cost_efficiency_score(cpi) * 0.15
    return round(pass_weight + inv_weight + min_weight + cost_weight, 2)


def _empty_leaderboard_entry(model_id: str, pricing: ModelPricing) -> ModelLeaderboardEntry:
    """Construct a blank leaderboard entry when no runs are present."""
    return ModelLeaderboardEntry(
        model_id=model_id,
        display_name=pricing.display_name,
        provider=pricing.provider,
        is_local=pricing.is_local,
        tasks_attempted=0,
        tasks_passed=0,
        pass_at_1=0.0,
        invariant_compliance_rate=0.0,
        headroom_rate=0.0,
        mean_complexity=0.0,
        mean_depth=0.0,
        mean_patch_minimality=0.0,
        total_cost_usd=0.0,
        cost_per_verified_task=0.0,
        cost_per_invariant=0.0,
        composite_score=0.0,
    )


def _compute_run_rates(runs: list[ScoredRun]) -> tuple[int, float, int, float, float]:
    """Calculate passed count, pass rate, invariant count, compliance rate, and headroom rate."""
    total = len(runs)
    passed_count = sum(1 for r in runs if r.tests_passed)
    pass_rate = round((passed_count / total) * 100.0, 1)
    tot_invs = sum(r.invariants_satisfied for r in runs)
    max_possible_invs = total * 6
    inv_rate = round((tot_invs / max_possible_invs) * 100.0, 1)
    headroom_count = sum(1 for r in runs if r.headroom_pass)
    headroom_rate = round((headroom_count / total) * 100.0, 1)
    return passed_count, pass_rate, tot_invs, inv_rate, headroom_rate


def _compute_run_averages(runs: list[ScoredRun]) -> tuple[float, float, float, float]:
    """Calculate mean complexity, mean depth, mean minimality, and total cost."""
    total = len(runs)
    mean_m = round(sum(r.max_complexity for r in runs) / total, 2)
    mean_d = round(sum(r.max_depth for r in runs) / total, 2)
    mean_min = round(sum(r.patch_minimality for r in runs) / total, 4)
    total_cost = round(sum(r.cost_usd for r in runs), 6)
    return mean_m, mean_d, mean_min, total_cost


def aggregate_model_runs(
    model_id: str, runs: list[ScoredRun], pricing: ModelPricing
) -> ModelLeaderboardEntry:
    """Aggregate individual scored runs into a leaderboard entry for a model."""
    if not runs:
        return _empty_leaderboard_entry(model_id, pricing)

    passed_count, pass_rate, tot_invs, inv_rate, headroom_rate = _compute_run_rates(runs)
    mean_m, mean_d, mean_min, total_cost = _compute_run_averages(runs)
    c_per_task = round(total_cost / max(1, passed_count), 4)
    c_per_inv = round(total_cost / max(1, tot_invs), 6)
    composite = _calculate_composite(pass_rate, inv_rate, mean_min, c_per_inv)

    return ModelLeaderboardEntry(
        model_id=model_id,
        display_name=pricing.display_name,
        provider=pricing.provider,
        is_local=pricing.is_local,
        tasks_attempted=len(runs),
        tasks_passed=passed_count,
        pass_at_1=pass_rate,
        invariant_compliance_rate=inv_rate,
        headroom_rate=headroom_rate,
        mean_complexity=mean_m,
        mean_depth=mean_d,
        mean_patch_minimality=mean_min,
        total_cost_usd=total_cost,
        cost_per_verified_task=c_per_task,
        cost_per_invariant=c_per_inv,
        composite_score=composite,
    )


def build_leaderboard(
    runs: list[ModelRun],
    catalog: dict[str, ModelPricing] | None = None,
) -> LeaderboardReport:
    """Build and rank a multi-model leaderboard from a collection of model runs."""
    pricing_catalog = catalog if catalog is not None else parse_pricing_catalog(DEFAULT_PRICING)
    scored_by_model: dict[str, list[ScoredRun]] = {}
    for run in runs:
        scored = score_run(run, pricing_catalog)
        scored_by_model.setdefault(run.model_id, []).append(scored)

    entries: list[ModelLeaderboardEntry] = []
    for model_id, model_runs in scored_by_model.items():
        pricing = pricing_catalog.get(
            model_id,
            ModelPricing(model_id, model_id, "Custom", 1.0, 3.0, False),
        )
        entry = aggregate_model_runs(model_id, model_runs, pricing)
        entries.append(entry)

    entries.sort(key=lambda e: e.composite_score, reverse=True)
    for idx, entry in enumerate(entries, start=1):
        entry.rank = idx

    now_iso = datetime.now(UTC).isoformat()
    return LeaderboardReport(
        timestamp=now_iso,
        total_runs=len(runs),
        models_evaluated=len(entries),
        entries=entries,
    )


def _format_markdown_quadrant(entries: Sequence[ModelLeaderboardEntry]) -> str:
    """Generate Mermaid quadrant chart for Cost vs Invariant Compliance."""
    lines = [
        "```mermaid",
        "quadrantChart",
        '    title "Multi-Model Efficiency: Cost vs Invariant Compliance"',
        '    x-axis "Low Token Cost / Open-Weights" --> "High Cloud Cost"',
        '    y-axis "Low Invariant Compliance" --> "High Invariant Compliance"',
        '    quadrant-1 "High-Cost Frontier"',
        '    quadrant-2 "Autonomous Sweet Spot"',
        '    quadrant-3 "Sub-Par / Sprawl"',
        '    quadrant-4 "Costly / Drift"',
    ]
    max_cpi = max((e.cost_per_invariant for e in entries), default=0.001)
    for e in entries:
        x_norm = round(min(0.95, max(0.05, e.cost_per_invariant / max(0.0001, max_cpi * 1.2))), 2)
        y_norm = round(min(0.95, max(0.05, e.invariant_compliance_rate / 100.0)), 2)
        lines.append(f'    "{e.display_name}": [{x_norm}, {y_norm}]')
    lines.append("```")
    return "\n".join(lines)


def format_markdown_report(report: LeaderboardReport) -> str:
    """Render a LeaderboardReport into formatted GitHub-flavored Markdown."""
    lines: list[str] = [
        "# 🏆 Multi-Model Benchmark Leaderboard & Cost-Per-Invariant Index",
        "",
        f"> **Generated**: `{report.timestamp}` | **Total Runs**: {report.total_runs} | **Models Evaluated**: {report.models_evaluated}",
        "",
        "## 1. Executive Summary & Rankings",
        "",
        "| Rank | Model | Provider | Pass@1 | Invariants % | Headroom % | Mean $M$ | Minimality | Total Cost ($) | Cost/Inv ($) | Composite |",
        "|:---:|---|---|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|",
    ]
    for e in report.entries:
        lines.append(
            f"| **{e.rank}** | **{e.display_name}** | {e.provider} | "
            f"{e.pass_at_1}% | {e.invariant_compliance_rate}% | {e.headroom_rate}% | "
            f"{e.mean_complexity} | {e.mean_patch_minimality} | "
            f"${e.total_cost_usd:.4f} | ${e.cost_per_invariant:.6f} | "
            f"**{e.composite_score}** |"
        )

    lines.extend(
        [
            "",
            "## 2. Cost-Per-Invariant Efficiency Matrix",
            "",
            _format_markdown_quadrant(report.entries),
            "",
            "## 3. Prescriptive Takeaways for Autonomous Swarms",
            "",
            "- **The Autonomous Sweet Spot (Quadrant 2)**: Models delivering high invariant compliance at a fraction of frontier API rates (e.g. DeepSeek-V3, Qwen-2.5-Coder-32B).",
            "- **Frontier Tier (Quadrant 1)**: Frontier reasoning models (Claude 3.5 Sonnet, GPT-4o) excel at initial multi-file architectural planning and high-complexity refactoring.",
            "- **Local Open-Weights Zero-Cost Typing**: Local models (Qwen-2.5-Coder-7B) eliminate cloud token expenditure entirely for atomic functions, typing, and single-test fixes.",
            "- **Patch Minimality & Diff Hygiene**: Surgical edits prevent maintenance debt, regression hazards, and code bloat in continuous delivery pipelines.",
        ]
    )
    return "\n".join(lines)


def format_json_report(report: LeaderboardReport) -> str:
    """Serialize a LeaderboardReport into indented JSON format."""
    data = asdict(report)
    return json.dumps(data, indent=2)


def generate_sample_benchmark_runs() -> list[ModelRun]:
    """Generate realistic empirical benchmark evaluation runs for sample testing."""
    sample_code_before = (
        "def process_items(items):\n"
        "    out = []\n"
        "    for x in items:\n"
        "        if x > 0:\n"
        "            out.append(x * 2)\n"
        "    return out\n"
    )
    sample_clean_after = (
        "def process_items(items: list[int]) -> list[int]:\n"
        '    """Process and double positive items in the input collection."""\n'
        "    return [x * 2 for x in items if x > 0]\n"
    )
    sample_sprawling_after = (
        "def process_items(items):\n"
        "    # Rewritten processing logic\n"
        "    out = []\n"
        "    for x in items:\n"
        "        if x is not None:\n"
        "            if x > 0:\n"
        "                val = x * 2\n"
        "                if val > 0:\n"
        "                    out.append(val)\n"
        "    return out\n"
    )

    models_data = [
        ("claude-3-5-sonnet", True, 1850, 420, 2.1, sample_clean_after),
        ("gpt-4o", True, 1920, 480, 2.4, sample_clean_after),
        ("deepseek-v3", True, 1780, 390, 1.8, sample_clean_after),
        ("qwen-2.5-coder-32b", True, 1750, 410, 1.6, sample_clean_after),
        ("qwen-2.5-coder-7b-local", True, 1600, 350, 1.2, sample_sprawling_after),
    ]

    runs: list[ModelRun] = []
    for model_id, passed, p_tok, c_tok, sec, code in models_data:
        runs.append(
            ModelRun(
                task_id="task-001-functional-refactor",
                model_id=model_id,
                tests_passed=passed,
                prompt_tokens=p_tok,
                completion_tokens=c_tok,
                execution_seconds=sec,
                code_before=sample_code_before,
                code_after=code,
            )
        )
    return runs


def _build_arg_parser() -> argparse.ArgumentParser:
    """Construct CLI argument parser for model leaderboard."""
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", help="Subcommand to execute")

    score_parser = subparsers.add_parser("score", help="Score an evaluation JSON file")
    score_parser.add_argument("input_path", type=Path, help="Path to evaluation JSON input file")
    score_parser.add_argument("--format", choices=["markdown", "json"], default="markdown")
    score_parser.add_argument("--output", type=Path, default=None, help="Optional output destination")

    sample_parser = subparsers.add_parser("sample", help="Run sample benchmark and generate leaderboard")
    sample_parser.add_argument("--format", choices=["markdown", "json"], default="markdown")
    sample_parser.add_argument("--output", type=Path, default=None, help="Optional output destination")

    minimality_parser = subparsers.add_parser("minimality", help="Compute patch minimality between two files")
    minimality_parser.add_argument("file_before", type=Path, help="Path to original file")
    minimality_parser.add_argument("file_after", type=Path, help="Path to modified file")

    return parser


def _emit_report(report: LeaderboardReport, format_choice: str, output: Path | None) -> int:
    """Emit formatted report to standard output or specified destination."""
    text = format_markdown_report(report) if format_choice == "markdown" else format_json_report(report)
    if output:
        output.write_text(text, encoding="utf-8")
        print(f"Report written to {output}")
    else:
        print(text)
    return 0


def _execute_sample(format_choice: str, output: Path | None) -> int:
    """Execute sample benchmark runs and output the formatted report."""
    runs = generate_sample_benchmark_runs()
    report = build_leaderboard(runs)
    return _emit_report(report, format_choice, output)


def _parse_model_run_item(item: Any) -> ModelRun | None:
    """Extract a ModelRun from a JSON dictionary item if valid."""
    if not isinstance(item, dict):
        return None
    return ModelRun(
        task_id=str(item.get("task_id", "unknown")),
        model_id=str(item.get("model_id", "unknown")),
        tests_passed=bool(item.get("tests_passed", False)),
        prompt_tokens=int(item.get("prompt_tokens", 0)),
        completion_tokens=int(item.get("completion_tokens", 0)),
        execution_seconds=float(item.get("execution_seconds", 0.0)),
        code_before=str(item.get("code_before", "")),
        code_after=str(item.get("code_after", "")),
        diff_text=str(item.get("diff_text", "")),
    )


def _load_runs_from_file(input_path: Path) -> list[ModelRun] | None:
    """Load model runs from a JSON file, returning None on failure."""
    if not input_path.exists():
        return None
    try:
        raw = json.loads(input_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    items = raw if isinstance(raw, list) else raw.get("runs", [])
    parsed = [_parse_model_run_item(it) for it in items]
    return [r for r in parsed if r is not None]


def _execute_score(input_path: Path, format_choice: str, output: Path | None) -> int:
    """Score a JSON file of runs and print or write report."""
    runs = _load_runs_from_file(input_path)
    if runs is None:
        print(f"Error reading JSON from {input_path}.", file=sys.stderr)
        return 1
    report = build_leaderboard(runs)
    return _emit_report(report, format_choice, output)


def _execute_minimality(file_before: Path, file_after: Path) -> int:
    """Compute and display patch minimality score between two files."""
    if not file_before.exists() or not file_after.exists():
        print("Error: one or both target files do not exist.", file=sys.stderr)
        return 1
    content_b = file_before.read_text(encoding="utf-8")
    content_a = file_after.read_text(encoding="utf-8")
    score = compute_patch_minimality(content_b, content_a)
    print(f"Patch Minimality: {score:.4f} (scale 0.0 to 1.0)")
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for Multi-Model Leaderboard."""
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    if args.command == "sample":
        return _execute_sample(args.format, args.output)
    if args.command == "score":
        return _execute_score(args.input_path, args.format, args.output)
    if args.command == "minimality":
        return _execute_minimality(args.file_before, args.file_after)

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
