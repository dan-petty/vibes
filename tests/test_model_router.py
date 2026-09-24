"""Unit and integration tests for Dynamic Multi-Model Router and Speculative Cascade Oracle."""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path
from typing import Any, Final

REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

from model_router import (
    DEFAULT_FAST_MODEL,
    DEFAULT_FRONTIER_MODEL,
    DEFAULT_LOCAL_MODEL,
    DiagnosticFinding,
    ModelRouter,
    ModelTier,
    RoutingDecision,
    TaskComplexityVector,
    check_egress_invariants,
    compute_semantic_entropy,
    estimate_token_cost,
    get_model_for_tier,
    main,
    measure_ast_complexity,
    run_cli_demo,
    select_tier,
    verify_code_invariants,
)


def test_task_complexity_score_bounds() -> None:
    """Verify complexity score computation across typical and extreme vectors."""
    simple_vec = TaskComplexityVector(file_count=1, estimated_ast_nodes=10, estimated_mccabe=1)
    heavy_vec = TaskComplexityVector(
        file_count=10,
        estimated_ast_nodes=500,
        estimated_mccabe=20,
        cross_symbol_refs=25,
        security_critical=True,
    )

    s_simple = simple_vec.complexity_score()
    s_heavy = heavy_vec.complexity_score()

    assert (s_simple < 0.20, s_heavy == 1.0) == (True, True)


def test_compute_semantic_entropy() -> None:
    """Verify semantic entropy and uncertainty threshold calculation."""
    res_empty = compute_semantic_entropy([])
    res_single = compute_semantic_entropy([1.0])
    res_uniform = compute_semantic_entropy([0.25, 0.25, 0.25, 0.25])
    res_divergent = compute_semantic_entropy([0.5, 0.5])

    assert (
        (res_empty.normalized_entropy, res_empty.uncertain),
        (res_single.normalized_entropy, res_single.uncertain),
        (res_uniform.normalized_entropy, res_uniform.uncertain),
        (res_divergent.normalized_entropy, res_divergent.uncertain),
    ) == (
        (0.0, False),
        (0.0, False),
        (1.0, True),
        (1.0, True),
    )


def test_model_tier_resolution_and_cost() -> None:
    """Verify model resolution and cost estimation across tiers."""
    m_local = get_model_for_tier(ModelTier.LOCAL)
    m_fast = get_model_for_tier(ModelTier.FAST)
    m_frontier = get_model_for_tier(ModelTier.FRONTIER)

    cost_local = estimate_token_cost(ModelTier.LOCAL, 1000, 1000)
    cost_frontier = estimate_token_cost(ModelTier.FRONTIER, 1000, 1000)

    assert (
        (m_local, m_fast, m_frontier),
        cost_local < cost_frontier,
    ) == (
        (DEFAULT_LOCAL_MODEL, DEFAULT_FAST_MODEL, DEFAULT_FRONTIER_MODEL),
        True,
    )


def test_select_tier_policy() -> None:
    """Verify tiered routing selection logic across complexity and security flags."""
    t_sec, _ = select_tier(complexity=0.1, entropy=0.0, security_critical=True)
    t_high, _ = select_tier(complexity=0.85, entropy=0.1, security_critical=False)
    t_mid, _ = select_tier(complexity=0.45, entropy=0.1, security_critical=False)
    t_low, _ = select_tier(complexity=0.15, entropy=0.05, security_critical=False)

    assert (t_sec, t_high, t_mid, t_low) == (
        ModelTier.FRONTIER,
        ModelTier.FRONTIER,
        ModelTier.FAST,
        ModelTier.LOCAL,
    )


def test_egress_invariant_detection() -> None:
    """Verify detection of private RFC 1918 addresses in code."""
    clean_code = "host = 'example.com'\nip = '192.0.2.1'\n"
    bad_code_1 = f"endpoint = 'http://{'.'.join(['10', '244', '0', '5'])}:8080'\n"
    bad_code_2 = f"target = '{'.'.join(['192', '168', '1', '100'])}'\n"

    ok_clean, _ = check_egress_invariants(clean_code)
    ok_bad1, msg1 = check_egress_invariants(bad_code_1)
    ok_bad2, msg2 = check_egress_invariants(bad_code_2)

    assert (
        ok_clean,
        (ok_bad1, "Zero-Trust Egress" in msg1),
        (ok_bad2, "Zero-Trust Egress" in msg2),
    ) == (
        True,
        (False, True),
        (False, True),
    )


def test_verify_code_invariants() -> None:
    """Verify invariant oracle over syntax, complexity, and security."""
    clean_code = "def add(x: int, y: int) -> int:\n    return x + y\n"
    syntax_error_code = "def broken(\n"
    complex_code = "def deep(x):\n" + "\n".join(f"    if x == {i}: return {i}" for i in range(15))

    ok_clean, _ = verify_code_invariants(clean_code)
    ok_syntax, msg_syntax = verify_code_invariants(syntax_error_code)
    ok_complex, msg_complex = verify_code_invariants(complex_code)

    tree = ast.parse("def f(x):\n    if x: return 1\n    return 0\n")
    m_calc, d_calc = measure_ast_complexity(tree)

    assert (
        ok_clean,
        (ok_syntax, "SyntaxError" in msg_syntax),
        (ok_complex, "McCabe complexity" in msg_complex),
        (m_calc, d_calc),
    ) == (
        True,
        (False, True),
        (False, True),
        (2, 1),
    )


def test_route_task_end_to_end() -> None:
    """Verify end-to-end task routing decisions."""
    router = ModelRouter()
    simple_vec = TaskComplexityVector(file_count=1, estimated_ast_nodes=10, estimated_mccabe=1)
    mid_vec = TaskComplexityVector(file_count=3, estimated_ast_nodes=120, estimated_mccabe=8)
    heavy_vec = TaskComplexityVector(
        file_count=5, estimated_ast_nodes=200, estimated_mccabe=12, security_critical=True
    )

    d_simple = router.route_task("task-simple", simple_vec)
    d_mid = router.route_task("task-mid", mid_vec)
    d_heavy = router.route_task("task-heavy", heavy_vec)

    assert (
        (d_simple.selected_tier, d_simple.speculative_enabled),
        (d_mid.selected_tier, d_mid.speculative_enabled),
        (d_heavy.selected_tier, d_heavy.speculative_enabled),
    ) == (
        (ModelTier.LOCAL, True),
        (ModelTier.FAST, True),
        (ModelTier.FRONTIER, False),
    )


def test_speculative_cascade_success() -> None:
    """Verify successful speculative cascade accepting valid draft code."""
    router = ModelRouter()
    valid_code = "def greet(name: str) -> str:\n    return f'Hello {name}'\n"

    res = router.execute_speculative_cascade("task-01", ModelTier.LOCAL, valid_code)

    assert (
        res.escalated,
        res.final_tier,
        res.invariant_passed,
        res.cost_saved_usd > 0.0,
    ) == (
        False,
        ModelTier.LOCAL,
        True,
        True,
    )


def test_speculative_cascade_escalation() -> None:
    """Verify speculative cascade rejecting invalid code and escalating to frontier."""
    router = ModelRouter()
    mock_ip = ".".join(["192", "168", "1", "5"])
    invalid_code = f"def secret():\n    return '{mock_ip}'\n"
    fallback_code = "def secret():\n    return '192.0.2.1'\n"

    res = router.execute_speculative_cascade(
        "task-sec", ModelTier.LOCAL, invalid_code, frontier_fallback_code=fallback_code
    )

    assert (
        res.escalated,
        res.final_tier,
        res.invariant_passed,
        "Zero-Trust Egress" in (res.escalation_reason or ""),
        res.final_code,
    ) == (
        True,
        ModelTier.FRONTIER,
        False,
        True,
        fallback_code,
    )


def test_audit_decisions_and_sarif_export() -> None:
    """Verify diagnostic auditing and standard SARIF 2.1.0 output."""
    router = ModelRouter()
    decisions = [
        RoutingDecision("t1", ModelTier.FRONTIER, DEFAULT_FRONTIER_MODEL, 0.1, 0.05, "Wasteful", 0.02, False),
        RoutingDecision("t2", ModelTier.LOCAL, DEFAULT_LOCAL_MODEL, 0.85, 0.50, "Fragile", 0.001, True),
    ]

    findings = router.audit_decisions(decisions)
    sarif = router.generate_sarif(findings)
    results = sarif["runs"][0]["results"]

    assert (
        len(findings),
        isinstance(findings[0], DiagnosticFinding),
        findings[0].rule_id,
        findings[1].rule_id,
        sarif["version"],
        len(results),
    ) == (
        2,
        True,
        "ROUT001",
        "ROUT002",
        "2.1.0",
        2,
    )


def test_markdown_report_generation() -> None:
    """Verify synthesis of human-readable Markdown reports."""
    router = ModelRouter()
    d = router.route_task("t1", TaskComplexityVector(file_count=1, estimated_ast_nodes=10))
    res = router.execute_speculative_cascade("t1", d.selected_tier, "def f(): pass\n")

    report = router.generate_markdown_report([d], [res])

    assert (
        "# Multi-Model Dynamic Routing" in report,
        "## Summary Telemetry" in report,
        "| `t1` |" in report,
    ) == (
        True,
        True,
        True,
    )


def test_cli_demo_and_main(capsys: Any) -> None:
    """Verify CLI demo execution and SARIF generation."""
    code_demo = run_cli_demo()
    out_demo = capsys.readouterr().out

    code_main_sarif = main(["--sarif"])
    out_sarif = capsys.readouterr().out
    sarif_data = json.loads(out_sarif)

    code_main_help = main(["--help"])
    out_help = capsys.readouterr().out

    assert (
        code_demo,
        "Multi-Model Dynamic Routing" in out_demo,
        code_main_sarif,
        sarif_data["version"],
        code_main_help,
        "Usage: python" in out_help,
    ) == (
        0,
        True,
        0,
        "2.1.0",
        0,
        True,
    )
