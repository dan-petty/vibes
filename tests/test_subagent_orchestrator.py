"""Tests for Hierarchical Subagent Slot Offloading Orchestrator.

Verifies token cost calculation, AST invariant verification, constraint envelope formatting,
subagent result evaluation, orchestration metrics, mock demo execution, and CLI interfaces.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

from subagent_orchestrator import (
    InvariantConstraint,
    SlotRole,
    SubagentOrchestrator,
    SubagentResult,
    SubagentTask,
    TaskStatus,
    calculate_cost,
    extract_public_symbols,
    format_constraint_envelope,
    format_json_report,
    format_markdown_report,
    main,
    verify_code_invariants,
)


def test_calculate_cost_paid_and_local() -> None:
    """Verify cost calculation for cloud reasoning models and free local models."""
    cost_claude = calculate_cost(10_000, 2_000, "claude-3-5-sonnet")
    cost_local = calculate_cost(50_000, 10_000, "qwen-2.5-coder-7b-local")
    expected_claude = (10_000 * 3.0 / 1_000_000.0) + (2_000 * 15.0 / 1_000_000.0)

    assert (round(cost_claude, 6), cost_local) == (round(expected_claude, 6), 0.0)


def test_extract_public_symbols() -> None:
    """Verify extraction of public functions and classes while ignoring private names."""
    code = (
        "def public_fn(): pass\n"
        "def _private_fn(): pass\n"
        "class PublicClass:\n"
        "    def method(self): pass\n"
        "class _PrivateClass: pass\n"
    )
    symbols = extract_public_symbols(code)
    invalid_symbols = extract_public_symbols("def broken(:")

    assert (symbols, invalid_symbols) == (("public_fn", "PublicClass"), ())


def test_verify_code_invariants_pass() -> None:
    """Verify that clean code passing complexity, depth, and symbol checks is approved."""
    code = (
        'def add(a: int, b: int) -> int:\n'
        '    """Sum two numbers."""\n'
        '    return a + b\n'
    )
    constraint = InvariantConstraint(
        constraint_id="INV-01",
        description="Check addition helper",
        max_complexity=5,
        max_depth=2,
        required_symbols=("add",),
    )
    passed, msg = verify_code_invariants(code, constraint)
    empty_pass, _ = verify_code_invariants("", constraint)

    assert (passed, msg, empty_pass) == (True, "All invariants verified.", True)


def test_verify_code_invariants_failures() -> None:
    """Verify rejection when complexity, depth, or required symbols breach constraints."""
    nested_code = (
        "def deep(items):\n"
        "    for x in items:\n"
        "        if x:\n"
        "            while x > 0:\n"
        "                for y in x:\n"
        "                    if y: pass\n"
    )
    constraint_depth = InvariantConstraint("INV-DEPTH", "Max depth 2", max_depth=2)
    constraint_comp = InvariantConstraint("INV-COMP", "Max complexity 2", max_complexity=2)
    constraint_sym = InvariantConstraint("INV-SYM", "Requires missing", required_symbols=("missing_fn",))

    pass_d, msg_d = verify_code_invariants(nested_code, constraint_depth)
    pass_c, msg_c = verify_code_invariants(nested_code, constraint_comp)
    pass_s, msg_s = verify_code_invariants("def existing(): pass\n", constraint_sym)
    pass_syntax, msg_syntax = verify_code_invariants("def broken(:", constraint_sym)

    assert (
        pass_d,
        pass_c,
        pass_s,
        pass_syntax,
        "exceeds ceiling" in msg_d,
        "exceeds ceiling" in msg_c,
        "missing required symbol" in msg_s,
        "SyntaxError" in msg_syntax,
    ) == (False, False, False, False, True, True, True, True)


def test_format_constraint_envelope() -> None:
    """Verify constraint envelope appends mandatory invariant requirements."""
    prompt = "Implement data parser."
    constraint = InvariantConstraint(
        constraint_id="INV-DATA-01",
        description="Preserve schema",
        max_complexity=6,
        max_depth=3,
        required_symbols=("parse_schema",),
    )
    envelope = format_constraint_envelope(prompt, (constraint,))
    raw_prompt = format_constraint_envelope(prompt, ())

    assert (
        "MANDATORY INVARIANT CONSTRAINTS" in envelope,
        "INV-DATA-01" in envelope,
        "Required Public Symbols: parse_schema" in envelope,
        raw_prompt,
    ) == (True, True, True, prompt)


def test_orchestrator_evaluate_task_result() -> None:
    """Verify task evaluation accepts clean outputs and rejects invariant violations."""
    orchestrator = SubagentOrchestrator()
    constraint = InvariantConstraint("INV-TEST", "Require run", required_symbols=("run",))
    task = SubagentTask(
        task_id="t1",
        title="Test task",
        role=SlotRole.CODE_TYPER,
        assigned_model="qwen-2.5-coder-32b",
        prompt="Write run function",
        constraints=(constraint,),
    )

    empty_status, empty_err = orchestrator.evaluate_task_result(task, "   ")
    valid_code = "def run() -> None: pass\n"
    valid_status, valid_err = orchestrator.evaluate_task_result(task, valid_code)
    invalid_code = "def other() -> None: pass\n"
    invalid_status, invalid_err = orchestrator.evaluate_task_result(task, invalid_code)

    assert (
        empty_status,
        valid_status,
        invalid_status,
        "empty response" in empty_err,
        valid_err,
        "violated" in invalid_err,
    ) == (
        TaskStatus.FAILED,
        TaskStatus.COMPLETED,
        TaskStatus.REJECTED,
        True,
        "",
        True,
    )


def test_orchestrator_calculate_metrics() -> None:
    """Verify aggregation of token tallies, offload efficiency, and cost savings."""
    orchestrator = SubagentOrchestrator(frontier_model="claude-3-5-sonnet")
    results = [
        SubagentResult("t1", SlotRole.PLANNER, "claude-3-5-sonnet", TaskStatus.COMPLETED, "plan", 1000, 200, 1.0),
        SubagentResult("t2", SlotRole.FILE_SCOUT, "qwen-2.5-coder-7b-local", TaskStatus.COMPLETED, "scout", 2000, 500, 0.5),
        SubagentResult("t3", SlotRole.CODE_TYPER, "qwen-2.5-coder-32b", TaskStatus.COMPLETED, "code", 3000, 600, 1.0),
        SubagentResult("t4", SlotRole.VERIFIER, "claude-3-5-sonnet", TaskStatus.COMPLETED, "audit", 800, 100, 0.4),
    ]

    metrics = orchestrator.calculate_metrics(results)
    frontier_tokens = (1000 + 200) + (800 + 100)
    offloaded_tokens = (2000 + 500) + (3000 + 600)
    total_tokens = frontier_tokens + offloaded_tokens

    assert (
        metrics.frontier_tokens,
        metrics.offloaded_tokens,
        metrics.total_tokens,
        metrics.offload_efficiency_ratio > 50.0,
        metrics.dollar_savings > 0.0,
        metrics.invariants_verified,
        metrics.invariants_violated,
    ) == (frontier_tokens, offloaded_tokens, total_tokens, True, True, 4, 0)


def test_execute_mock_demo() -> None:
    """Verify end-to-end mock demo execution runs all slots cleanly."""
    orchestrator = SubagentOrchestrator()
    record = orchestrator.execute_mock_demo()

    assert (
        record.plan_id,
        record.status,
        len(record.results),
        record.metrics.invariants_verified,
        record.metrics.invariants_violated,
        record.metrics.dollar_savings > 0.0,
    ) == ("demo-slot-offload", TaskStatus.COMPLETED, 4, 4, 0, True)


def test_format_markdown_and_json_reports() -> None:
    """Verify markdown and json report formatting contains required sections."""
    orchestrator = SubagentOrchestrator()
    record = orchestrator.execute_mock_demo()

    md_report = format_markdown_report(record)
    json_report = format_json_report(record)
    parsed_json = json.loads(json_report)

    assert (
        "# 🤖 Hierarchical Subagent Slot Offloading Report" in md_report,
        "sequenceDiagram" in md_report,
        "Offload Efficiency" in md_report,
        parsed_json["plan_id"],
        len(parsed_json["results"]),
    ) == (True, True, True, "demo-slot-offload", 4)


def test_cli_demo_execution(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    """Verify CLI demo subcommand outputs markdown or json to stdout and file."""
    out_file = tmp_path / "offload.json"
    code_out = main(["demo"])
    captured = capsys.readouterr()

    code_file = main(["demo", "--format", "json", "--output", str(out_file)])

    assert (
        code_out,
        code_file,
        "# 🤖 Hierarchical Subagent Slot Offloading Report" in captured.out,
        out_file.exists(),
    ) == (0, 0, True, True)


def test_cli_verify_execution(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    """Verify CLI verify subcommand audits Python files against invariants."""
    clean_file = tmp_path / "clean.py"
    clean_file.write_text("def fn() -> int: return 42\n", encoding="utf-8")
    missing_file = tmp_path / "missing.py"

    code_ok = main(["verify", str(clean_file)])
    cap_ok = capsys.readouterr()

    code_missing = main(["verify", str(missing_file)])
    cap_missing = capsys.readouterr()

    assert (
        code_ok,
        code_missing,
        "passed invariant checks" in cap_ok.out,
        "not found" in cap_missing.err,
    ) == (0, 1, True, True)
