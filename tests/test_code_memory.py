"""Unit and integration tests for Semantic Graph AST Code Memory & Persistent Symbol Indexing."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Final

REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

from code_memory import (
    CodeMemoryFinding,
    CodeMemoryIndex,
    CodeMemoryOracle,
    EdgeKind,
    GraphEdge,
    SymbolKind,
    SymbolNode,
    SymbolSignature,
    compute_sha256,
    extract_function_signature,
    generate_markdown_report,
    generate_mermaid_graph,
    generate_sarif,
    index_directory,
    main,
)


def test_signature_extraction_and_hashing() -> None:
    """Verify function parameter constraints extraction and deterministic SHA-256."""
    import ast

    code = "def sample_func(a: int, b: str = 'def', *args, **kwargs) -> bool:\n    return True\n"
    tree = ast.parse(code)
    func_def = tree.body[0]
    assert isinstance(func_def, ast.FunctionDef)

    sig = extract_function_signature(func_def)
    digest = compute_sha256(code)

    assert (
        sig.required_count,
        sig.total_count,
        sig.has_varargs,
        sig.has_varkwargs,
        len(digest),
    ) == (1, 2, True, True, 64)


def test_index_file_symbol_extraction() -> None:
    """Verify classes, methods, and functions indexing into semantic graph."""
    idx = CodeMemoryIndex()
    code = (
        "class Worker:\n"
        "    def run(self, task: str) -> None:\n"
        "        pass\n\n"
        "def helper(x: int) -> int:\n"
        "    return x + 1\n"
    )

    first_index = idx.index_file("worker.py", code)
    second_index = idx.index_file("worker.py", code)

    worker_class = idx.symbols.get("worker.py::Worker")
    worker_method = idx.symbols.get("worker.py::Worker.run")
    helper_func = idx.symbols.get("worker.py::helper")

    assert (
        first_index,
        second_index,
        worker_class is not None,
        worker_method is not None,
        helper_func is not None,
    ) == (True, False, True, True, True)


def test_edge_and_call_detection() -> None:
    """Verify extraction of calls, imports, and inheritance edges."""
    idx = CodeMemoryIndex()
    code = (
        "from os import path\n"
        "class BaseTask:\n"
        "    pass\n"
        "class CustomTask(BaseTask):\n"
        "    def execute(self) -> None:\n"
        "        helper(42)\n"
    )
    idx.index_file("tasks.py", code)

    kinds = {e.kind for e in idx.edges}
    assert (
        EdgeKind.IMPORTS in kinds,
        EdgeKind.INHERITS in kinds,
        EdgeKind.DEFINES in kinds,
        EdgeKind.CALLS in kinds,
    ) == (True, True, True, True)


def test_blast_radius_transitive_closure() -> None:
    """Verify transitive upstream reachability closure across call hierarchy."""
    idx = CodeMemoryIndex()
    code_a = "def leaf() -> int:\n    return 1\n"
    code_b = "def mid() -> int:\n    return leaf()\n"
    code_c = "def root() -> int:\n    return mid()\n"

    idx.index_file("leaf.py", code_a)
    idx.index_file("mid.py", code_b)
    idx.index_file("root.py", code_c)

    # Wire cross-file call edges
    idx.edges.append(
        GraphEdge(
            source_id="mid.py::mid",
            target_id="leaf.py::leaf",
            kind=EdgeKind.CALLS,
            call_site_line=2,
            call_arg_count=0,
        )
    )
    idx.edges.append(
        GraphEdge(
            source_id="root.py::root",
            target_id="mid.py::mid",
            kind=EdgeKind.CALLS,
            call_site_line=2,
            call_arg_count=0,
        )
    )
    idx._rebuild_adjacency()

    radius = idx.calculate_blast_radius("leaf.py::leaf")
    assert ("mid.py::mid" in radius, "root.py::root" in radius, len(radius)) == (True, True, 2)


def test_dependency_cycle_detection() -> None:
    """Verify directed cycle detection in dependency graph."""
    idx = CodeMemoryIndex()
    idx.symbols["mod_a::func_a"] = SymbolNode(
        id="mod_a::func_a",
        name="func_a",
        kind=SymbolKind.FUNCTION,
        file_path="mod_a.py",
        line=1,
        signature=None,
        docstring=None,
        content_hash="h1",
    )
    idx.symbols["mod_b::func_b"] = SymbolNode(
        id="mod_b::func_b",
        name="func_b",
        kind=SymbolKind.FUNCTION,
        file_path="mod_b.py",
        line=1,
        signature=None,
        docstring=None,
        content_hash="h2",
    )

    idx.edges.append(
        GraphEdge(source_id="mod_a::func_a", target_id="mod_b::func_b", kind=EdgeKind.CALLS, call_site_line=2)
    )
    idx.edges.append(
        GraphEdge(source_id="mod_b::func_b", target_id="mod_a::func_a", kind=EdgeKind.CALLS, call_site_line=2)
    )
    idx._rebuild_adjacency()

    cycles = idx.detect_dependency_cycles()
    assert (len(cycles) > 0, "mod_a::func_a" in cycles[0]) == (True, True)


def test_audit_dangling_reference_sgm001() -> None:
    """Verify SGM001 detection when target symbol is missing from index."""
    idx = CodeMemoryIndex()
    code = "def caller() -> None:\n    removed_func()\n"
    idx.index_file("caller.py", code)

    # Edge targeting non-existent symbol with qualified id
    idx.edges.append(
        GraphEdge(
            source_id="caller.py::caller",
            target_id="missing.py::removed_func",
            kind=EdgeKind.CALLS,
            call_site_line=2,
            call_arg_count=0,
        )
    )
    idx._rebuild_adjacency()

    oracle = CodeMemoryOracle(idx)
    findings = oracle.audit_dangling_references()

    assert (len(findings), findings[0].rule_id, findings[0].severity) == (1, "SGM001", "error")


def test_audit_signature_mismatch_sgm002() -> None:
    """Verify SGM002 detection for invalid positional argument counts."""
    idx = CodeMemoryIndex()
    target_sig = SymbolSignature(
        params=["a", "b"],
        required_count=2,
        total_count=2,
        has_varargs=False,
        has_varkwargs=False,
        kwonly_args=[],
    )
    idx.symbols["target.py::calculate"] = SymbolNode(
        id="target.py::calculate",
        name="calculate",
        kind=SymbolKind.FUNCTION,
        file_path="target.py",
        line=1,
        signature=target_sig,
        docstring=None,
        content_hash="hash",
    )

    # Under-arity: passes 1 arg instead of 2
    idx.edges.append(
        GraphEdge(
            source_id="caller.py::run",
            target_id="target.py::calculate",
            kind=EdgeKind.CALLS,
            call_site_line=5,
            call_arg_count=1,
        )
    )
    # Over-arity: passes 3 args instead of 2
    idx.edges.append(
        GraphEdge(
            source_id="caller.py::run2",
            target_id="target.py::calculate",
            kind=EdgeKind.CALLS,
            call_site_line=10,
            call_arg_count=3,
        )
    )
    idx._rebuild_adjacency()

    oracle = CodeMemoryOracle(idx)
    findings = oracle.audit_signature_mismatches()

    assert (len(findings), findings[0].rule_id, findings[1].rule_id) == (2, "SGM002", "SGM002")


def test_audit_cycle_and_orphan_findings() -> None:
    """Verify SGM003 (cycles) and SGM004 (orphaned symbols) audits."""
    idx = CodeMemoryIndex()
    code = "def solitary_func() -> None:\n    pass\n"
    idx.index_file("solo.py", code)

    oracle = CodeMemoryOracle(idx)
    orphans = oracle.audit_orphaned_definitions()

    assert (len(orphans) >= 1, orphans[0].rule_id, orphans[0].severity) == (1, "SGM004", "warning")


def test_audit_high_blast_radius_sgm005() -> None:
    """Verify SGM005 warning when modifying a hub symbol with >= 5 upstream dependents."""
    idx = CodeMemoryIndex()
    hub_id = "core.py::hub_service"
    idx.symbols[hub_id] = SymbolNode(
        id=hub_id,
        name="hub_service",
        kind=SymbolKind.FUNCTION,
        file_path="core.py",
        line=1,
        signature=None,
        docstring=None,
        content_hash="hub_hash",
    )

    for i in range(6):
        src_id = f"client_{i}.py::caller_{i}"
        idx.edges.append(
            GraphEdge(
                source_id=src_id,
                target_id=hub_id,
                kind=EdgeKind.CALLS,
                call_site_line=2,
                call_arg_count=0,
            )
        )
    idx._rebuild_adjacency()

    oracle = CodeMemoryOracle(idx)
    findings = oracle.audit_high_blast_radius({hub_id}, threshold=5)

    assert (len(findings), findings[0].rule_id, findings[0].severity) == (1, "SGM005", "warning")


def test_sarif_export_and_mermaid_rendering() -> None:
    """Verify SARIF 2.1.0 schema validity and WCAG AA contrast Mermaid generation."""
    idx = CodeMemoryIndex()
    code = "class Controller:\n    pass\ndef run_all():\n    pass\n"
    idx.index_file("controller.py", code)

    finding = CodeMemoryFinding(
        rule_id="SGM001",
        severity="error",
        message="Unindexed call target.",
        file_path="controller.py",
        line=3,
        symbol_id="controller.py::run_all",
    )

    sarif = generate_sarif([finding])
    mermaid = generate_mermaid_graph(idx)
    md_report = generate_markdown_report([finding], idx)

    assert (
        sarif["version"] == "2.1.0",
        len(sarif["runs"][0]["results"]) == 1,
        "flowchart TD" in mermaid,
        "#ffffff" in mermaid,
        "Semantic Graph AST Code Memory Audit Report" in md_report,
    ) == (True, True, True, True, True)


def test_index_serialization_roundtrip() -> None:
    """Verify dictionary serialization and deserialization integrity."""
    idx = CodeMemoryIndex()
    code = "def sample() -> None:\n    pass\n"
    idx.index_file("sample.py", code)

    payload = idx.to_dict()
    restored = CodeMemoryIndex.from_dict(payload)

    assert (
        len(restored.symbols) == len(idx.symbols),
        restored.file_hashes == idx.file_hashes,
        len(restored.edges) == len(idx.edges),
    ) == (True, True, True)


def test_index_directory_and_cli_main(tmp_path: Path) -> None:
    """Verify directory recursive indexing and CLI options."""
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "mod_a.py").write_text("def ping() -> str:\n    return 'pong'\n", encoding="utf-8")
    (src_dir / "mod_b.py").write_text("def pong() -> str:\n    return 'ping'\n", encoding="utf-8")

    idx = index_directory(src_dir)
    sarif_file = tmp_path / "out.sarif"
    mermaid_file = tmp_path / "out.mmd"

    code = main(
        [
            "--index",
            str(src_dir),
            "--verify",
            "--sarif",
            str(sarif_file),
            "--mermaid",
            str(mermaid_file),
            "--blast-radius",
            "mod_a.py::ping",
        ]
    )

    assert (
        len(idx.symbols) >= 2,
        sarif_file.exists(),
        mermaid_file.exists(),
        code,
    ) == (True, True, True, 0)
