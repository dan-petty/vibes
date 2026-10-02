"""Unit and invariant test suite for Closed-Loop PR Review Thread Synchronizer.

Validates thread parsing, heuristic rule classification, AST symbol localization,
mechanical fix verification, SARIF 2.1.0 telemetry generation, and GraphQL mutation synthesis.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

from pr_thread_sync import (
    ResolutionStatus,
    ReviewComment,
    ReviewThread,
    ThreadAnalysis,
    classify_comment_rule,
    compute_ast_complexity,
    export_sarif,
    generate_resolution_mutation,
    locate_ast_symbol,
    main,
    parse_review_threads,
    summarize_analyses,
    synchronize_threads,
    verify_thread_resolution,
)


def test_thread_data_structures_and_parsing() -> None:
    """Verifies review comment dataclass and thread JSON payload deserialization."""
    payload = [
        {
            "id": "PRRT_kwDO12345",
            "isResolved": False,
            "path": "src/module.py",
            "line": 42,
            "comments": [
                {
                    "id": "PRRC_kwDO98765",
                    "author": {"login": "ci-bot"},
                    "body": "Nesting depth is 4 (limit: 3)",
                    "createdAt": "2026-10-01T12:00:00Z",
                }
            ],
        },
        {
            "id": "PRRT_kwDO67890",
            "isResolved": True,
            "path": "docs/guide.md",
            "line": 10,
            "comments": [],
        },
    ]

    threads = parse_review_threads(json.dumps(payload))
    t1, t2 = threads[0], threads[1]

    assert (
        len(threads),
        t1.thread_id,
        t1.is_resolved,
        t1.path,
        t1.line,
        len(t1.comments),
        t1.comments[0].author,
        t1.comments[0].body,
        t2.thread_id,
        t2.is_resolved,
        len(t2.comments),
    ) == (
        2,
        "PRRT_kwDO12345",
        False,
        "src/module.py",
        42,
        1,
        "ci-bot",
        "Nesting depth is 4 (limit: 3)",
        "PRRT_kwDO67890",
        True,
        0,
    )


def test_classify_comment_rule() -> None:
    """Verifies heuristic rule classification from review comment bodies."""
    c_res = classify_comment_rule("Function has cyclomatic complexity of 8 (limit: 6)")
    n_res = classify_comment_rule("Function has nesting depth of 4 (limit: 3)")
    z_res = classify_comment_rule("Zero-trust leak: RFC 1918 private IP detected")
    d_res = classify_comment_rule("Missing docstring on public function")
    t_res = classify_comment_rule("Type annotation missing on function argument")
    g_res = classify_comment_rule("Consider refactoring this design approach.")

    assert (c_res, n_res, z_res, d_res, t_res, g_res) == (
        "CC001",
        "ND001",
        "SEC001",
        "DOC001",
        "TYP001",
        "GENERAL",
    )


def test_locate_ast_symbol_and_complexity(tmp_path: Path) -> None:
    """Verifies enclosing AST symbol location and cyclomatic complexity computation."""
    source_code = (
        "class Calculator:\n"
        "    def compute(self, x: int) -> int:\n"
        "        if x > 10:\n"
        "            return x * 2\n"
        "        return x + 1\n"
    )
    py_file = tmp_path / "calc.py"
    py_file.write_text(source_code, encoding="utf-8")

    sym_calc = locate_ast_symbol(py_file, 1)
    sym_comp = locate_ast_symbol(py_file, 4)
    sym_none = locate_ast_symbol(tmp_path / "missing.py", 10)
    sym_md = locate_ast_symbol(tmp_path / "doc.md", 5)

    assert (sym_calc, sym_comp, sym_none, sym_md) == (
        "class 'Calculator' (lines 1-5)",
        "function 'compute' (lines 2-5)",
        "file scope",
        "file scope",
    )

    import ast

    tree = ast.parse(source_code)
    func_node = tree.body[0].body[0]  # type: ignore[attr-defined]
    complexity = compute_ast_complexity(func_node)
    assert complexity == 2


def test_verify_thread_resolution_flow(tmp_path: Path) -> None:
    """Verifies end-to-end resolution state verification for various audit conditions."""
    resolved_thread = ReviewThread(
        thread_id="T_RESOLVED",
        is_resolved=True,
        path="src/dummy.py",
        line=1,
        comments=[],
    )
    res_resolved = verify_thread_resolution(tmp_path, resolved_thread)
    assert (res_resolved.status, res_resolved.rule_id) == (ResolutionStatus.RESOLVED, "NONE")

    orphaned_thread = ReviewThread(
        thread_id="T_ORPHAN",
        is_resolved=False,
        path="src/deleted.py",
        line=5,
        comments=[ReviewComment("C1", "bot", "Fix this issue", "2026-10-01T00:00:00Z")],
    )
    res_orphaned = verify_thread_resolution(tmp_path, orphaned_thread)
    assert (res_orphaned.status, res_orphaned.rule_id) == (ResolutionStatus.ORPHANED_PATH, "ORPHAN")

    py_file = tmp_path / "code.py"
    py_file.write_text("def simple():\n    return 42\n", encoding="utf-8")
    clean_thread = ReviewThread(
        thread_id="T_CLEAN",
        is_resolved=False,
        path="code.py",
        line=1,
        comments=[ReviewComment("C2", "bot", "cyclomatic complexity of 8", "2026-10-01T00:00:00Z")],
    )
    res_clean = verify_thread_resolution(tmp_path, clean_thread)
    batch_res = synchronize_threads(tmp_path, [resolved_thread, orphaned_thread, clean_thread])
    assert (res_clean.status, res_clean.rule_id, len(batch_res)) == (
        ResolutionStatus.FIX_VERIFIED,
        "CC001",
        3,
    )


def test_graphql_mutations_and_sarif_telemetry() -> None:
    """Verifies synthesis of atomic GraphQL mutations and valid OASIS SARIF 2.1.0 output."""
    mutation = generate_resolution_mutation("PRRT_test123", "Fixed cleanly.")
    assert 'resolveReviewThread(input: { threadId: "PRRT_test123" })' in mutation
    assert "Fixed cleanly." in mutation

    thread1 = ReviewThread("T1", False, "a.py", 10, [ReviewComment("C1", "bot", "nesting depth of 5", "t")])
    thread2 = ReviewThread("T2", False, "b.py", 20, [ReviewComment("C2", "bot", "bad idea", "t")])

    analyses = [
        ThreadAnalysis(thread1, ResolutionStatus.FIX_VERIFIED, "ND001", "fn", "Fixed", "Resolved!"),
        ThreadAnalysis(thread2, ResolutionStatus.ACTION_REQUIRED, "GENERAL", "file", "Not fixed", ""),
    ]

    sarif = export_sarif(analyses)
    run = sarif["runs"][0]
    results = run["results"]
    summary = summarize_analyses(analyses)

    assert (sarif["version"], len(results), results[0]["ruleId"]) == ("2.1.0", 2, "PR_THREAD_ND001")
    assert (summary["total"], summary["fix_verified"], summary["action_required"]) == (2, 1, 1)


def test_cli_execution_with_fixture(tmp_path: Path, capsys: object) -> None:
    """Verifies CLI execution, file output writing, and exit code handling."""
    code_path = tmp_path / "sample.py"
    code_path.write_text("def ok():\n    return True\n", encoding="utf-8")

    threads_data = [
        {
            "id": "PRRT_SAMPLE_1",
            "isResolved": False,
            "path": "sample.py",
            "line": 1,
            "comments": [
                {"id": "C1", "author": {"login": "bot"}, "body": "Nesting depth is 4", "createdAt": "t"}
            ],
        }
    ]
    json_input = tmp_path / "threads.json"
    json_input.write_text(json.dumps(threads_data), encoding="utf-8")
    sarif_output = tmp_path / "output.sarif"

    exit_code = main(["--input", str(json_input), "--root", str(tmp_path), "--sarif", str(sarif_output)])
    sarif_exists = sarif_output.exists()
    assert (exit_code, sarif_exists) == (0, True)
