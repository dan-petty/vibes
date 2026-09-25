"""Unit and integration test suite for Autonomous 3-Way AST Semantic Reconciler.

Validates commutative disjoint symbol reconciliation, import sets unification,
class method arbitration, and semantic collision detection under SARIF 2.1.0.

Certified compliant with AST Invariant Sentinel (M <= 4, depth <= 2).
"""

from __future__ import annotations

import ast
from pathlib import Path

from reconciler import (
    ReconciliationStatus,
    SymbolKind,
    classify_statement_symbol,
    extract_docstring_node,
    hash_ast_node,
    main,
    parse_ast_safely,
    reconcile_3way,
    reconcile_import_sets,
    to_markdown,
    to_sarif,
)


def test_hash_ast_node_and_safe_parse() -> None:
    """Verify deterministic structural hashing and safe AST parsing."""
    code_a = "def foo(x: int) -> int:\n    return x + 1\n"
    code_b = "def foo(x: int) -> int:\n    return x + 1  # comment\n"
    tree_a = parse_ast_safely(code_a)
    tree_b = parse_ast_safely(code_b)
    assert (
        hash_ast_node(tree_a.body[0]),
        hash_ast_node(tree_b.body[0]),
    ) == (
        hash_ast_node(tree_b.body[0]),
        hash_ast_node(tree_a.body[0]),
    )


def test_extract_docstring_and_classification() -> None:
    """Verify docstring separation and symbol classification."""
    code = '"""Module docstring."""\n\nFLAG: bool = True\ndef task(): pass\n'
    tree = parse_ast_safely(code)
    doc_node, rest = extract_docstring_node(tree)
    name_c, kind_c = classify_statement_symbol(rest[0], 0)
    name_f, kind_f = classify_statement_symbol(rest[1], 1)
    assert (
        doc_node is not None,
        len(rest),
        name_c,
        kind_c,
        name_f,
        kind_f,
    ) == (
        True,
        2,
        "FLAG",
        SymbolKind.CONSTANT,
        "task",
        SymbolKind.FUNCTION,
    )


def test_reconcile_import_sets_deduplication() -> None:
    """Verify 3-way import union and false conflict counting."""
    base_imp = {"import os", "from sys import version"}
    ours_imp = {"import os", "from sys import version", "import json"}
    theirs_imp = {"import os", "from sys import version", "import time"}
    merged, false_conflicts = reconcile_import_sets(base_imp, ours_imp, theirs_imp)
    expected = ["from sys import version", "import json", "import os", "import time"]
    assert (merged, false_conflicts) == (expected, 1)


def test_reconcile_trivial_clean_merge() -> None:
    """Verify clean 3-way merge when only one branch modifies a function."""
    base = "def worker():\n    return 0\n"
    ours = "def worker():\n    return 42\n"
    theirs = "def worker():\n    return 0\n"
    res = reconcile_3way(base, ours, theirs)
    assert (
        res.status,
        "return 42" in res.merged_code,
        len(res.collisions),
    ) == (
        ReconciliationStatus.CLEAN_MERGE,
        True,
        0,
    )


def test_reconcile_commutative_disjoint_functions() -> None:
    """Verify commutative merge of disjoint functions added to the same position."""
    base = "def common():\n    return 1\n"
    ours = "def common():\n    return 1\n\n\ndef helper_alpha():\n    return 'alpha'\n"
    theirs = "def common():\n    return 1\n\n\ndef helper_beta():\n    return 'beta'\n"
    res = reconcile_3way(base, ours, theirs)
    parsed = ast.parse(res.merged_code)
    func_names = [stmt.name for stmt in parsed.body if isinstance(stmt, ast.FunctionDef)]
    assert (
        res.status,
        res.resolved_false_conflicts >= 1,
        "helper_alpha" in func_names,
        "helper_beta" in func_names,
    ) == (
        ReconciliationStatus.COMMUTATIVE_MERGE,
        True,
        True,
        True,
    )


def test_reconcile_class_disjoint_methods() -> None:
    """Verify method-level reconciliation when branches add disjoint methods."""
    base = "class Service:\n    def start(self):\n        pass\n"
    ours = "class Service:\n    def start(self):\n        pass\n    def pause(self):\n        pass\n"
    theirs = "class Service:\n    def start(self):\n        pass\n    def resume(self):\n        pass\n"
    res = reconcile_3way(base, ours, theirs)
    assert (
        res.status,
        len(res.collisions),
        "def pause" in res.merged_code,
        "def resume" in res.merged_code,
    ) == (
        ReconciliationStatus.COMMUTATIVE_MERGE,
        0,
        True,
        True,
    )


def test_reconcile_semantic_collision_reporting() -> None:
    """Verify diagnostic detection when both branches make incompatible edits."""
    base = "def calculate(x: int) -> int:\n    return x\n"
    ours = "def calculate(x: int) -> int:\n    return x * 2\n"
    theirs = "def calculate(x: int) -> int:\n    return x * 3\n"
    res = reconcile_3way(base, ours, theirs)
    col = res.collisions[0] if res.collisions else None
    assert (
        res.status,
        len(res.collisions),
        col.symbol_name if col else "",
        col.symbol_kind if col else None,
    ) == (
        ReconciliationStatus.SEMANTIC_COLLISION,
        1,
        "calculate",
        SymbolKind.FUNCTION,
    )


def test_sarif_and_markdown_exporters() -> None:
    """Verify SARIF 2.1.0 schema contract and Markdown summary formatting."""
    base = "def f(): pass\n"
    ours = "def f(): return 1\n"
    theirs = "def f(): return 2\n"
    res = reconcile_3way(base, ours, theirs)
    sarif = to_sarif(res)
    md = to_markdown(res)
    assert (
        sarif["version"],
        len(sarif["runs"][0]["results"]),
        "SEMANTIC_COLLISION" in md,
        "❌ COLLISION" in md,
    ) == (
        "2.1.0",
        1,
        True,
        True,
    )


def test_main_cli_execution_clean(tmp_path: Path) -> None:
    """Verify main CLI command execution writes merged output to file."""
    base_file = tmp_path / "base.py"
    ours_file = tmp_path / "ours.py"
    theirs_file = tmp_path / "theirs.py"
    out_file = tmp_path / "out.py"

    base_file.write_text("x = 1\n", encoding="utf-8")
    ours_file.write_text("x = 1\ndef fa(): pass\n", encoding="utf-8")
    theirs_file.write_text("x = 1\ndef fb(): pass\n", encoding="utf-8")

    code = main([
        "--base", str(base_file),
        "--ours", str(ours_file),
        "--theirs", str(theirs_file),
        "--output", str(out_file),
    ])
    assert (code, out_file.exists(), "def fa" in out_file.read_text(encoding="utf-8")) == (0, True, True)


def test_main_cli_execution_collision(tmp_path: Path) -> None:
    """Verify main CLI command returns exit code 1 on collision."""
    base_file = tmp_path / "base.py"
    ours_file = tmp_path / "ours.py"
    theirs_file = tmp_path / "theirs.py"

    base_file.write_text("def run(): return 0\n", encoding="utf-8")
    ours_file.write_text("def run(): return 1\n", encoding="utf-8")
    theirs_file.write_text("def run(): return 2\n", encoding="utf-8")

    code = main([
        "--base", str(base_file),
        "--ours", str(ours_file),
        "--theirs", str(theirs_file),
        "--format", "markdown",
    ])
    assert code == 1
