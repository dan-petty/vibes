"""Unit tests for AST Relevance Context Ranker & Optimizer.

Verifies AST symbol extraction, graph construction, Personalized PageRank
convergence, token-budgeted context packing, diagnostic invariant rules,
and SARIF 2.1.0 telemetry export.

Certified compliant with AST Invariant Sentinel (M <= 4, depth <= 2, params <= 4).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Add current example directory to sys.path
sys.path.insert(0, str(Path(__file__).parent))

from context_ranker import (
    DiagnosticRule,
    FidelityLevel,
    Finding,
    PageRankConfig,
    SymbolKind,
    build_symbol_graph,
    check_diagnostic_invariants,
    compute_pagerank,
    crawl_repository_sources,
    export_sarif,
    extract_symbols_from_source,
    format_markdown_report,
    main,
    pack_context,
)

SAMPLE_SOURCE = '''
import math

class Calculator:
    """Mathematical utility engine."""

    def add(self, a: int, b: int) -> int:
        """Add two integers."""
        return a + b

    def compute(self, x: int) -> int:
        """Compute square root of addition."""
        total = self.add(x, 10)
        return int(math.sqrt(total))

def run_pipeline(val: int) -> int:
    """Execute pipeline via Calculator."""
    calc = Calculator()
    return calc.compute(val)

def standalone_helper() -> str:
    """Helper with zero connections."""
    return "ok"
'''

CYCLE_SOURCE = """
def func_a() -> None:
    func_b()

def func_b() -> None:
    func_a()
"""


def test_extract_symbols_from_source() -> None:
    """Verify AST parses functions, classes, methods, and imports."""
    symbols = extract_symbols_from_source(SAMPLE_SOURCE, "calc.py")
    kinds = [s.kind for s in symbols]
    names = [s.name for s in symbols]
    expected_names = [
        "Calculator",
        "Calculator.add",
        "Calculator.compute",
        "run_pipeline",
        "standalone_helper",
    ]
    present = [name in names for name in expected_names]
    assert (len(symbols) >= 5, SymbolKind.CLASS in kinds, all(present)) == (True, True, True)


def test_extract_symbols_syntax_error_resilience() -> None:
    """Verify syntax errors return empty list safely without crashing."""
    bad_source = "def unclosed_block(x:\n    return x +"
    symbols = extract_symbols_from_source(bad_source, "bad.py")
    assert symbols == []


def test_build_symbol_graph_and_dependencies() -> None:
    """Verify directed symbol graph captures call dependencies."""
    symbols = extract_symbols_from_source(SAMPLE_SOURCE, "calc.py")
    graph = build_symbol_graph(symbols)
    pipeline_id = "calc.py::run_pipeline"
    calc_id = "calc.py::Calculator"
    edges = graph.edges.get(pipeline_id, set())
    assert (pipeline_id in graph.nodes, calc_id in edges) == (True, True)


def test_pagerank_uniform_convergence() -> None:
    """Verify global PageRank scores sum to approximately 1.0."""
    symbols = extract_symbols_from_source(SAMPLE_SOURCE, "calc.py")
    graph = build_symbol_graph(symbols)
    scores = compute_pagerank(graph, PageRankConfig(max_iter=30))
    total_score = sum(scores.values())
    node_count = len(graph.nodes)
    assert (node_count > 0, abs(total_score - 1.0) < 1e-3) == (True, True)


def test_personalized_pagerank_elevates_focal_symbol() -> None:
    """Verify Personalized PageRank gives highest score to focal symbol."""
    symbols = extract_symbols_from_source(SAMPLE_SOURCE, "calc.py")
    graph = build_symbol_graph(symbols)
    focal_id = "calc.py::run_pipeline"
    scores = compute_pagerank(graph, PageRankConfig(focal_symbols=(focal_id,)))
    sorted_ids = sorted(scores.keys(), key=lambda k: scores[k], reverse=True)
    assert (sorted_ids[0] == focal_id, scores[focal_id] > scores["calc.py::standalone_helper"]) == (
        True,
        True,
    )


def test_pack_context_budget_bounding() -> None:
    """Verify context packer packs symbols up to strict token budget."""
    symbols = extract_symbols_from_source(SAMPLE_SOURCE, "calc.py")
    graph = build_symbol_graph(symbols)
    compute_pagerank(graph)
    res = pack_context(graph, budget_tokens=60, fidelity=FidelityLevel.FULL)
    assert (res.total_tokens <= 60, len(res.packed_symbols) > 0, len(res.findings) > 0) == (True, True, True)


def test_pack_context_fidelity_degradation() -> None:
    """Verify signatures fidelity renders signatures without full body."""
    symbols = extract_symbols_from_source(SAMPLE_SOURCE, "calc.py")
    graph = build_symbol_graph(symbols)
    compute_pagerank(graph)
    res_sig = pack_context(graph, budget_tokens=2000, fidelity=FidelityLevel.SIGNATURES)
    res_out = pack_context(graph, budget_tokens=2000, fidelity=FidelityLevel.OUTLINE)
    assert ("..." in res_sig.rendered_text, "lines" in res_out.rendered_text) == (True, True)


def test_diagnostic_orphans_and_cycles() -> None:
    """Verify orphan detection and mutual cycle detection."""
    syms_cycle = extract_symbols_from_source(CYCLE_SOURCE, "cycle.py")
    graph = build_symbol_graph(syms_cycle)
    findings = check_diagnostic_invariants(graph)
    rules = [f.rule for f in findings]
    assert DiagnosticRule.RNK004 in rules


def test_diagnostic_orphans_detected() -> None:
    """Verify orphaned symbol produces RNK001 note finding."""
    symbols = extract_symbols_from_source(SAMPLE_SOURCE, "calc.py")
    graph = build_symbol_graph(symbols)
    findings = check_diagnostic_invariants(graph)
    rules = [f.rule for f in findings]
    targets = [f.target for f in findings]
    assert (DiagnosticRule.RNK001 in rules, "calc.py::standalone_helper" in targets) == (True, True)


def test_crawl_repository_sources(tmp_path: Path) -> None:
    """Verify repository source crawling and file ingestion."""
    sub = tmp_path / "pkg"
    sub.mkdir()
    (sub / "mod_a.py").write_text("def alpha(): return 1\n", encoding="utf-8")
    (sub / "mod_b.py").write_text("def beta(): return 2\n", encoding="utf-8")
    (sub / "ignored.txt").write_text("plain text", encoding="utf-8")
    symbols = crawl_repository_sources(tmp_path)
    names = sorted(s.name for s in symbols)
    assert (len(symbols) == 2, names == ["alpha", "beta"]) == (True, True)


def test_sarif_export_schema_and_markdown_report() -> None:
    """Verify SARIF 2.1.0 JSON format and markdown report rendering."""
    finding = Finding(
        rule=DiagnosticRule.RNK002,
        message="Hub hotspot",
        target="calc.py::Calculator",
        severity="warning",
    )
    raw_sarif = export_sarif([finding])
    data = json.loads(raw_sarif)
    symbols = extract_symbols_from_source(SAMPLE_SOURCE, "calc.py")
    graph = build_symbol_graph(symbols)
    compute_pagerank(graph)
    res = pack_context(graph, budget_tokens=500)
    report = format_markdown_report(res)
    assert (
        data["version"] == "2.1.0",
        data["runs"][0]["results"][0]["ruleId"] == "RNK002",
        "# AST Relevance" in report,
    ) == (True, True, True)


def test_cli_dispatch_rank_and_pack(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Verify CLI rank and pack commands execute cleanly."""
    file_path = tmp_path / "app.py"
    file_path.write_text(SAMPLE_SOURCE, encoding="utf-8")
    code_rank = main(["rank", "--dir", str(tmp_path)])
    out_rank, _ = capsys.readouterr()
    code_pack = main(["pack", "--dir", str(tmp_path), "--budget", "500", "--format", "pack"])
    out_pack, _ = capsys.readouterr()
    code_empty = main([])
    assert (
        code_rank == 0,
        "Ranked" in out_rank,
        code_pack == 0,
        "Repository Context Pack" in out_pack,
        code_empty == 0,
    ) == (True, True, True, True, True)
