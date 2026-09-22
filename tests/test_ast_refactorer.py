"""Unit tests for the Automated AST Conditional Refactorer.

Verifies:
- McCabe cyclomatic complexity calculation and indentation nesting depth tracking.
- Comparison operator inversion mapping.
- Detection and transformation of equality ladders into table-driven dictionary dispatch.
- Flattening deeply nested conditionals into early-return guard clauses.
- Extracting compound boolean expressions into pure predicate helper functions.
- Consolidating sequential test assertions into tuple equality checks.
- Invariant safety gate rejection of malformed or regressive transformations.
- CLI argument parsing, unified diff generation, and feedback backlog ingestion.
"""

from __future__ import annotations

import ast
import json
import sys
import tempfile
from pathlib import Path

# Add repo root and tools/ to sys.path for direct imports
_repo_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_repo_root / "tools"))
sys.path.insert(0, str(_repo_root))

import pytest
from ast_refactorer import (
    ASTRefactorer,
    CandidateDetector,
    RefactorCandidate,
    RefactorStrategy,
    calculate_cyclomatic_complexity,
    calculate_nesting_depth,
    invert_condition,
    run_cli,
)

SAMPLE_LADDER_SOURCE = """
def get_service_port(protocol: str) -> int:
    if protocol == "http":
        return 80
    elif protocol == "https":
        return 443
    elif protocol == "ssh":
        return 22
    elif protocol == "dns":
        return 53
    elif protocol == "ntp":
        return 123
    elif protocol == "smtp":
        return 25
    elif protocol == "redis":
        return 6379
    else:
        return 0
"""

SAMPLE_NESTED_GUARD_SOURCE = """
def process_user_record(user: dict | None) -> str | None:
    if user is not None:
        name = user.get("name")
        if name:
            return name.strip().lower()
    return None
"""

SAMPLE_COMPOUND_BOOL_SOURCE = """
def is_packet_admissible(version: int, payload_len: int, flag: bool) -> bool:
    if version == 4 and payload_len > 0 and flag:
        return True
    return False
"""

SAMPLE_TEST_ASSERTS_SOURCE = """
def test_mock_response():
    resp_code = 200
    resp_status = "OK"
    resp_body = "payload"
    assert resp_code == 200
    assert resp_status == "OK"
    assert resp_body == "payload"
"""


def test_complexity_and_nesting_calculation() -> None:
    """Verify calculation of cyclomatic complexity and nesting depth on AST node."""
    tree = ast.parse(SAMPLE_LADDER_SOURCE)
    fn = tree.body[0]
    complexity = calculate_cyclomatic_complexity(fn)
    depth = calculate_nesting_depth(fn)
    assert (complexity, depth) == (8, 8)


def test_invert_condition() -> None:
    """Verify inversion of comparison operations and boolean expressions."""
    cmp_eq = ast.parse("x == 10").body[0].value  # type: ignore[attr-defined]
    cmp_is_not = ast.parse("x is not None").body[0].value  # type: ignore[attr-defined]
    not_flag = ast.parse("not ready").body[0].value  # type: ignore[attr-defined]
    inv_eq = ast.unparse(invert_condition(cmp_eq))
    inv_is_not = ast.unparse(invert_condition(cmp_is_not))
    inv_not_flag = ast.unparse(invert_condition(not_flag))
    assert (inv_eq, inv_is_not, inv_not_flag) == ("x != 10", "x is None", "ready")


def test_detect_equality_ladder() -> None:
    """Verify detection of equality ladders and target variable extraction."""
    tree = ast.parse(SAMPLE_LADDER_SOURCE)
    fn = tree.body[0]
    ladder = CandidateDetector._find_ladder_in_body(fn)  # type: ignore[arg-type]
    assert ladder is not None
    target_id, branches, fallback = ladder
    assert (target_id, len(branches), ast.unparse(fallback)) == ("protocol", 7, "0")


def test_refactor_table_dispatch() -> None:
    """Verify transforming equality ladder into table dispatch with behavioral equivalence."""
    refactorer = ASTRefactorer(threshold=7)
    candidates = refactorer.scan_source(SAMPLE_LADDER_SOURCE, "ports.py")
    assert (
        len(candidates),
        candidates[0].suggested_strategy,
        candidates[0].initial_complexity,
    ) == (1, RefactorStrategy.TABLE_DISPATCH, 8)

    result = refactorer.refactor_source(SAMPLE_LADDER_SOURCE, candidates[0])
    assert (
        result.success,
        result.final_complexity,
        result.complexity_delta,
        "_GET_SERVICE_PORT_DISPATCH" in result.refactored_code,
        ".get(protocol, 0)" in result.refactored_code,
    ) == (True, 1, 7, True, True)

    scope_orig: dict[str, object] = {}
    scope_refactored: dict[str, object] = {}
    exec(SAMPLE_LADDER_SOURCE, scope_orig)
    exec(result.refactored_code, scope_refactored)

    fn_orig = scope_orig["get_service_port"]
    fn_refactored = scope_refactored["get_service_port"]
    test_keys = ["http", "https", "ssh", "dns", "redis", "unknown"]
    assert [fn_orig(k) for k in test_keys] == [fn_refactored(k) for k in test_keys]


def test_detect_and_refactor_guard_clause() -> None:
    """Verify flattening nested conditionals into early-return guard clauses."""
    refactorer = ASTRefactorer(threshold=1)
    candidates = refactorer.scan_source(SAMPLE_NESTED_GUARD_SOURCE, "users.py")
    matching = [c for c in candidates if c.suggested_strategy == RefactorStrategy.GUARD_CLAUSE_FLATTEN]
    assert len(matching) >= 1

    result = refactorer.refactor_source(SAMPLE_NESTED_GUARD_SOURCE, matching[0])
    assert (result.success, "if user is None:" in result.refactored_code) == (True, True)

    scope_orig: dict[str, object] = {}
    scope_refactored: dict[str, object] = {}
    exec(SAMPLE_NESTED_GUARD_SOURCE, scope_orig)
    exec(result.refactored_code, scope_refactored)

    fn_orig = scope_orig["process_user_record"]
    fn_refactored = scope_refactored["process_user_record"]
    test_cases = [None, {"name": " Alice "}, {"name": ""}]
    assert [fn_orig(u) for u in test_cases] == [fn_refactored(u) for u in test_cases]


def test_detect_and_refactor_compound_boolean() -> None:
    """Verify extracting compound boolean conditions into pure predicate helper functions."""
    refactorer = ASTRefactorer(threshold=1)
    candidates = refactorer.scan_source(SAMPLE_COMPOUND_BOOL_SOURCE, "packet.py")
    pred_candidates = [c for c in candidates if c.suggested_strategy == RefactorStrategy.PREDICATE_EXTRACTION]
    assert len(pred_candidates) >= 1

    result = refactorer.refactor_source(SAMPLE_COMPOUND_BOOL_SOURCE, pred_candidates[0])
    assert (result.success, "_is_is_packet_admissible_valid" in result.refactored_code) == (True, True)

    scope_orig: dict[str, object] = {}
    scope_refactored: dict[str, object] = {}
    exec(SAMPLE_COMPOUND_BOOL_SOURCE, scope_orig)
    exec(result.refactored_code, scope_refactored)

    fn_orig = scope_orig["is_packet_admissible"]
    fn_refactored = scope_refactored["is_packet_admissible"]
    test_tuples = [(4, 100, True), (4, 0, True), (6, 50, True), (4, 10, False)]
    assert [fn_orig(*t) for t in test_tuples] == [fn_refactored(*t) for t in test_tuples]


def test_detect_and_refactor_assertion_consolidation() -> None:
    """Verify consolidating consecutive simple assert statements into tuple assertions."""
    refactorer = ASTRefactorer(threshold=1)
    candidates = refactorer.scan_source(SAMPLE_TEST_ASSERTS_SOURCE, "test_demo.py")
    assert_candidates = [c for c in candidates if c.suggested_strategy == RefactorStrategy.ASSERTION_CONSOLIDATION]
    assert len(assert_candidates) >= 1

    result = refactorer.refactor_source(SAMPLE_TEST_ASSERTS_SOURCE, assert_candidates[0])
    expected_assert = "assert (resp_code, resp_status, resp_body) == (200, 'OK', 'payload')"
    assert (result.success, expected_assert in result.refactored_code) == (True, True)


def test_safety_verifier_rejection() -> None:
    """Verify invariant safety gate rejects invalid syntax or non-applicable refactorings."""
    refactorer = ASTRefactorer(threshold=7)
    dummy_candidate = RefactorCandidate(
        file_path="dummy.py",
        function_name="dummy_func",
        lineno=1,
        initial_complexity=3,
        initial_depth=1,
        suggested_strategy=RefactorStrategy.TABLE_DISPATCH,
        details="dummy",
    )
    res_syntax = refactorer.refactor_source("def dummy_func():\n    syntax error !!", dummy_candidate)
    res_not_applicable = refactorer.refactor_source("def dummy_func():\n    return 42", dummy_candidate)
    assert (res_syntax.success, res_not_applicable.success) == (False, False)


def test_cli_scan_and_diff(capsys: pytest.CaptureFixture[str]) -> None:
    """Verify CLI scan, diff generation, and error handling across input flags."""
    with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False) as f:
        f.write(SAMPLE_LADDER_SOURCE)
        temp_path = Path(f.name)

    try:
        ret_scan = run_cli(["--file", str(temp_path), "--scan", "--threshold", "7"])
        captured_scan = capsys.readouterr()
        assert (ret_scan, "TABLE_DISPATCH" in captured_scan.out) == (0, True)

        ret_diff = run_cli(["--file", str(temp_path), "--diff", "--threshold", "7"])
        captured_diff = capsys.readouterr()
        assert (ret_diff, "--- a/" in captured_diff.out, "+++ b/" in captured_diff.out) == (0, True, True)
    finally:
        temp_path.unlink(missing_ok=True)


def test_feedback_backlog_ingestion(capsys: pytest.CaptureFixture[str]) -> None:
    """Verify processing and refactoring targets from SDLC feedback backlog JSON."""
    with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False) as f_src:
        f_src.write(SAMPLE_LADDER_SOURCE)
        temp_py = Path(f_src.name)

    feedback_data = [
        {
            "category": "PROACTIVE_REFACTOR",
            "target": str(temp_py),
            "headline": "Refactor test target",
        }
    ]
    with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f_json:
        json.dump(feedback_data, f_json)
        temp_json = Path(f_json.name)

    try:
        ret = run_cli(["--from-feedback", str(temp_json), "--threshold", "7"])
        captured = capsys.readouterr()
        assert (ret, "Successful: 1" in captured.out, "Net M reduction: 7" in captured.out) == (0, True, True)
    finally:
        temp_py.unlink(missing_ok=True)
        temp_json.unlink(missing_ok=True)
