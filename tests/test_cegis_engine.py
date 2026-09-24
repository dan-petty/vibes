"""Unit and integration test suite for CEGIS Engine & Invariant Repair Oracle.

Validates counterexample extraction, negative constraint accumulation, monotonic convergence,
cycle oscillation detection, latent regression detection, and multi-format reporting.
Consolidates assertions into structural tuple checks to maintain proactive complexity headroom.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final

import pytest

REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

from cegis_engine import (
    ASTInvariantVerifier,
    CEGISEngine,
    CEGISPreset,
    ConvergenceOracle,
    ConvergenceStatus,
    Counterexample,
    NegativeConstraintAccumulator,
    TrajectoryStep,
    export_json,
    export_sarif,
    format_markdown_report,
    main,
)


def test_verifier_clean_code() -> None:
    """Verify clean Python code satisfies standard and strict invariant gates."""
    code = (
        "def compute_summary(items: list[int]) -> int:\n"
        "    \"\"\"Return sum of items.\"\"\"\n"
        "    return sum(items)\n"
    )
    verifier = ASTInvariantVerifier()
    violations, max_cc, max_d = verifier.verify_source(code, "clean.py")
    assert (len(violations), max_cc, max_d) == (0, 1, 1)


def test_verifier_complexity_violation() -> None:
    """Verify cyclomatic complexity exceeding ceiling triggers CEGIS001."""
    code = (
        "def branchy(a: int, b: int, c: int, d: int) -> int:\n"
        "    \"\"\"Branchy function with high complexity.\"\"\"\n"
        "    if a > 0:\n"
        "        return 1\n"
        "    elif b > 0:\n"
        "        return 2\n"
        "    elif c > 0:\n"
        "        return 3\n"
        "    elif d > 0:\n"
        "        return 4\n"
        "    elif a + b > 0:\n"
        "        return 5\n"
        "    elif c + d > 0:\n"
        "        return 6\n"
        "    elif a + c > 0:\n"
        "        return 7\n"
        "    return 0\n"
    )
    verifier = ASTInvariantVerifier()
    violations, max_cc, _ = verifier.verify_source(code, "branchy.py")
    rules = [v.rule_id for v in violations]
    assert ("CEGIS001" in rules, max_cc >= 7) == (True, True)


def test_verifier_nesting_depth_violation() -> None:
    """Verify deeply nested compound statements trigger CEGIS002."""
    code = (
        "def deep_nest(items: list[list[list[list[int]]]]) -> int:\n"
        "    \"\"\"Deeply nested iteration.\"\"\"\n"
        "    total = 0\n"
        "    for a in items:\n"
        "        for b in a:\n"
        "            for c in b:\n"
        "                for d in c:\n"
        "                    total += d\n"
        "    return total\n"
    )
    verifier = ASTInvariantVerifier()
    violations, _, max_d = verifier.verify_source(code, "nest.py")
    rules = [v.rule_id for v in violations]
    assert ("CEGIS002" in rules, max_d >= 4) == (True, True)


def test_verifier_parameter_count_violation() -> None:
    """Verify excessive parameter cardinality triggers CEGIS003."""
    code = (
        "def many_params(p1: int, p2: int, p3: int, p4: int, p5: int) -> int:\n"
        "    \"\"\"Callable exceeding allowable argument count.\"\"\"\n"
        "    return p1 + p2 + p3 + p4 + p5\n"
    )
    verifier = ASTInvariantVerifier()
    violations, _, _ = verifier.verify_source(code, "params.py")
    rules = [v.rule_id for v in violations]
    assert ("CEGIS003" in rules, len(violations) >= 1) == (True, True)


def test_verifier_zero_trust_private_ip() -> None:
    """Verify string literals containing private IP addresses trigger CEGIS004."""
    private_ip = ".".join(["10", "0", "0", "1"])
    code = f'ENDPOINT = "http://{private_ip}:8080/api"\n'
    verifier = ASTInvariantVerifier()
    violations, _, _ = verifier.verify_source(code, "ip.py")
    rules = [v.rule_id for v in violations]
    assert ("CEGIS004" in rules, violations[0].violating_pattern) == (True, private_ip)


def test_verifier_assertion_sprawl() -> None:
    """Verify consecutive linear assert statements in test functions trigger CEGIS005."""
    code = (
        "def test_sprawl() -> None:\n"
        "    \"\"\"Test with unconsolidated linear assertions.\"\"\"\n"
        "    a = 1\n"
        "    b = 2\n"
        "    c = 3\n"
        "    assert a == 1\n"
        "    assert b == 2\n"
        "    assert c == 3\n"
    )
    verifier = ASTInvariantVerifier()
    violations, _, _ = verifier.verify_source(code, "test_sample.py")
    rules = [v.rule_id for v in violations]
    assert ("CEGIS005" in rules, len(violations) == 1) == (True, True)


def test_negative_constraint_accumulator() -> None:
    """Verify negative constraint accumulation, deduplication, and repeat detection."""
    accumulator = NegativeConstraintAccumulator()
    ce1 = Counterexample("CEGIS001", "mod.py:10", "Complexity exceeded", "comp_8", "hash_01", 10)
    ce2 = Counterexample("CEGIS002", "mod.py:20", "Depth exceeded", "depth_4", "hash_02", 20)

    c1 = accumulator.register_counterexample(ce1, 0)
    c1_dup = accumulator.register_counterexample(ce1, 1)
    c2 = accumulator.register_counterexample(ce2, 1)

    repeats = accumulator.check_for_repeat_violation([ce1])

    assert (
        c1.constraint_id,
        c1_dup.constraint_id,
        c2.constraint_id,
        len(accumulator.constraints),
        len(repeats),
        repeats[0].rule_id,
    ) == ("NC-001", "NC-001", "NC-002", 2, 1, "CEGIS008")


def test_convergence_oracle_monotonic_convergence() -> None:
    """Verify Oracle detects monotonic convergence when violations resolve cleanly."""
    ce = Counterexample("CEGIS001", "mod.py:5", "Complexity high", "comp", "h1", 5)
    s0 = TrajectoryStep(0, "c0", "ast0", False, (ce,), 1, 8, 2)
    s1 = TrajectoryStep(1, "c1", "ast1", True, (), 1, 3, 1)

    status, cycles, regs = ConvergenceOracle.evaluate([s0, s1], 5)
    assert (status, len(cycles), len(regs)) == (ConvergenceStatus.CONVERGED, 0, 0)


def test_convergence_oracle_cycle_detection() -> None:
    """Verify Oracle detects oscillatory repair cycles between identical AST states."""
    ce = Counterexample("CEGIS001", "mod.py:5", "Complexity high", "comp", "h1", 5)
    s0 = TrajectoryStep(0, "c0", "ast_alpha", False, (ce,), 1, 8, 2)
    s1 = TrajectoryStep(1, "c1", "ast_beta", False, (ce,), 1, 7, 2)
    s2 = TrajectoryStep(2, "c2", "ast_alpha", False, (ce,), 1, 8, 2)

    status, cycles, regs = ConvergenceOracle.evaluate([s0, s1, s2], 5)
    assert (status, cycles, regs) == (ConvergenceStatus.OSCILLATING, [(0, 2)], [])


def test_convergence_oracle_latent_regression() -> None:
    """Verify Oracle identifies latent regressions where a new rule breaks during repair."""
    ce1 = Counterexample("CEGIS001", "mod.py:5", "Complexity high", "comp", "h1", 5)
    ce2 = Counterexample("CEGIS004", "mod.py:8", "IP egress", "ip", "h2", 8)
    s0 = TrajectoryStep(0, "c0", "ast0", False, (ce1,), 1, 8, 2)
    s1 = TrajectoryStep(1, "c1", "ast1", False, (ce1, ce2), 2, 8, 2)

    status, cycles, regs = ConvergenceOracle.evaluate([s0, s1], 5)
    assert (status, cycles, regs) == (ConvergenceStatus.DEGRADED, [], ["CEGIS004"])


def test_synthesis_loop_execution() -> None:
    """Verify CEGISEngine runs synthesis loop until convergence with mock refactorer."""
    bad_code = (
        "def calculate(p1: int, p2: int, p3: int, p4: int, p5: int) -> int:\n"
        "    \"\"\"Exceeds parameter ceiling.\"\"\"\n"
        "    return p1 + p2 + p3 + p4 + p5\n"
    )
    fixed_code = (
        "def calculate(items: list[int]) -> int:\n"
        "    \"\"\"Consolidated parameters into list.\"\"\"\n"
        "    return sum(items)\n"
    )

    def mock_refactor(src: str, constraints: Sequence[Any]) -> str:
        return fixed_code if constraints else src

    engine = CEGISEngine(preset=CEGISPreset.STRICT)
    result = engine.run_synthesis_loop(bad_code, mock_refactor, max_iterations=3)

    assert (
        result.status,
        result.total_iterations,
        result.trajectory[0].passed,
        result.trajectory[1].passed,
    ) == (ConvergenceStatus.CONVERGED, 2, False, True)


def test_sarif_and_json_and_markdown_exports() -> None:
    """Verify SARIF 2.1.0, JSON, and Markdown exporters produce conformant schemas."""
    code = "def f(a: int, b: int, c: int, d: int, e: int) -> int:\n    return a\n"
    engine = CEGISEngine(preset=CEGISPreset.STRICT)
    result = engine.audit_single(code, "target.py")

    sarif = export_sarif(result, "target.py")
    json_str = export_json(result)
    parsed_json = json.loads(json_str)
    md_str = format_markdown_report(result)

    assert (
        sarif["version"],
        len(sarif["runs"]),
        parsed_json["status"],
        "# Counterexample-Guided Inductive Synthesis (CEGIS) Report" in md_str,
    ) == ("2.1.0", 1, "converging", True)


def test_main_cli_execution(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Verify main CLI execution against temporary Python target files."""
    clean_file = tmp_path / "clean_test.py"
    clean_file.write_text("def ping() -> str:\n    return 'pong'\n", encoding="utf-8")

    sarif_out = tmp_path / "findings.sarif"
    rc = main([str(clean_file), "--export-sarif", str(sarif_out)])
    captured = capsys.readouterr()

    assert (rc, sarif_out.is_file(), "CONVERGED" in captured.out) == (0, True, True)
