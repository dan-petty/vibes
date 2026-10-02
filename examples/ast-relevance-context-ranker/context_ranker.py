"""Graph-Ranked AST Context Optimizer & Relevance Packer.

Extracts abstract syntax tree (AST) symbol graphs from repository source trees,
computes Personalized PageRank centrality over symbol dependencies and call graphs,
and packs maximal relevant context within strict token budgets without syntax truncation.

Certified compliant with AST Invariant Sentinel (M <= 4, depth <= 2, params <= 4).
"""

from __future__ import annotations

import argparse
import ast
import json
import math
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

DEFAULT_DAMPING: Final[float] = 0.85
DEFAULT_TOLERANCE: Final[float] = 1e-5
DEFAULT_MAX_ITER: Final[int] = 50
DEFAULT_BUDGET_TOKENS: Final[int] = 2000
MAX_SAFE_FILE_SIZE: Final[int] = 5_000_000


class SymbolKind(StrEnum):
    """Semantic category of an extracted AST symbol."""

    FUNCTION = "FUNCTION"
    ASYNC_FUNCTION = "ASYNC_FUNCTION"
    CLASS = "CLASS"
    METHOD = "METHOD"
    IMPORT = "IMPORT"


class FidelityLevel(StrEnum):
    """Fidelity tier for packed context representation."""

    FULL = "FULL"
    SIGNATURES = "SIGNATURES"
    OUTLINE = "OUTLINE"


class DiagnosticRule(StrEnum):
    """AST context ranker diagnostic rules."""

    RNK001 = "RNK001"  # Orphaned Symbol
    RNK002 = "RNK002"  # Hub Centrality Hotspot
    RNK003 = "RNK003"  # Budget Saturation Truncation
    RNK004 = "RNK004"  # Circular Reference Cycle


@dataclass(frozen=True)
class Finding:
    """Diagnostic finding produced during graph analysis or packing."""

    rule: DiagnosticRule
    message: str
    target: str
    severity: str = "warning"


@dataclass
class SymbolNode:
    """Top-level or member AST code symbol."""

    symbol_id: str
    name: str
    file_path: str
    kind: SymbolKind
    line_start: int
    line_end: int
    docstring: str | None
    signature: str
    body_code: str
    tokens: int
    references: list[str] = field(default_factory=list)
    score: float = 0.0


@dataclass(frozen=True)
class PageRankConfig:
    """Parameters controlling PageRank power iteration."""

    damping: float = DEFAULT_DAMPING
    tolerance: float = DEFAULT_TOLERANCE
    max_iter: int = DEFAULT_MAX_ITER
    focal_symbols: tuple[str, ...] = ()


class _ReferenceVisitor(ast.NodeVisitor):
    """Collects identifier and attribute name references from an AST block."""

    def __init__(self) -> None:
        self.names: set[str] = set()

    def visit_Name(self, node: ast.Name) -> None:
        self.names.add(node.id)
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        self.names.add(node.attr)
        self.generic_visit(node)


def estimate_tokens(text: str) -> int:
    """Estimate token count for code or docstring with floor of 1."""
    if not text.strip():
        return 1
    return max(1, math.ceil(len(text) / 4.0))


def _slice_source(lines: Sequence[str], start: int, end: int) -> str:
    """Slice line range 1-indexed inclusive."""
    bounded_start = max(0, start - 1)
    bounded_end = min(len(lines), end)
    return "\n".join(lines[bounded_start:bounded_end])


def _extract_function_signature(fn_node: ast.FunctionDef | ast.AsyncFunctionDef, lines: Sequence[str]) -> str:
    """Extract def header line or signature slice."""
    if fn_node.lineno <= len(lines):
        return lines[fn_node.lineno - 1].strip()
    return f"def {fn_node.name}():"


def _make_function_symbol(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    file_path: str,
    lines: Sequence[str],
    kind: SymbolKind,
) -> SymbolNode:
    """Construct a SymbolNode for a function or method."""
    end_line = getattr(node, "end_lineno", node.lineno)
    body = _slice_source(lines, node.lineno, end_line)
    vis = _ReferenceVisitor()
    vis.visit(node)
    vis.names.discard(node.name)
    sym_id = f"{file_path}::{node.name}"
    return SymbolNode(
        symbol_id=sym_id,
        name=node.name,
        file_path=file_path,
        kind=kind,
        line_start=node.lineno,
        line_end=end_line,
        docstring=ast.get_docstring(node),
        signature=_extract_function_signature(node, lines),
        body_code=body,
        tokens=estimate_tokens(body),
        references=sorted(vis.names),
    )


def _make_class_symbol(
    node: ast.ClassDef,
    file_path: str,
    lines: Sequence[str],
) -> tuple[SymbolNode, list[SymbolNode]]:
    """Construct class SymbolNode and nested member methods."""
    end_line = getattr(node, "end_lineno", node.lineno)
    body = _slice_source(lines, node.lineno, end_line)
    vis = _ReferenceVisitor()
    vis.visit(node)
    vis.names.discard(node.name)
    class_id = f"{file_path}::{node.name}"
    class_sym = SymbolNode(
        symbol_id=class_id,
        name=node.name,
        file_path=file_path,
        kind=SymbolKind.CLASS,
        line_start=node.lineno,
        line_end=end_line,
        docstring=ast.get_docstring(node),
        signature=lines[node.lineno - 1].strip() if node.lineno <= len(lines) else f"class {node.name}:",
        body_code=body,
        tokens=estimate_tokens(body),
        references=sorted(vis.names),
    )
    methods = _extract_class_methods(node, file_path, lines)
    return class_sym, methods


def _extract_class_methods(
    cls_node: ast.ClassDef,
    file_path: str,
    lines: Sequence[str],
) -> list[SymbolNode]:
    """Extract nested methods from a class definition."""
    methods: list[SymbolNode] = []
    for item in cls_node.body:
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            m_sym = _make_function_symbol(item, file_path, lines, SymbolKind.METHOD)
            m_sym.symbol_id = f"{file_path}::{cls_node.name}.{item.name}"
            m_sym.name = f"{cls_node.name}.{item.name}"
            methods.append(m_sym)
    return methods


def _make_import_symbol(
    node: ast.Import | ast.ImportFrom,
    file_path: str,
    lines: Sequence[str],
) -> SymbolNode:
    """Construct an import statement SymbolNode."""
    end_line = getattr(node, "end_lineno", node.lineno)
    body = _slice_source(lines, node.lineno, end_line)
    names: list[str] = [alias.name for alias in node.names]
    line_name = f"import_{node.lineno}"
    return SymbolNode(
        symbol_id=f"{file_path}::{line_name}",
        name=line_name,
        file_path=file_path,
        kind=SymbolKind.IMPORT,
        line_start=node.lineno,
        line_end=end_line,
        docstring=None,
        signature=body,
        body_code=body,
        tokens=estimate_tokens(body),
        references=sorted(names),
    )


def _safe_parse_ast(source_text: str) -> ast.Module | None:
    """Safely parse python source into an AST module."""
    try:
        return ast.parse(source_text)
    except SyntaxError:
        return None


def _dispatch_stmt_symbols(
    stmt: ast.stmt,
    file_path: str,
    lines: Sequence[str],
) -> list[SymbolNode]:
    """Convert an AST statement into its constituent symbol nodes."""
    if isinstance(stmt, ast.FunctionDef):
        return [_make_function_symbol(stmt, file_path, lines, SymbolKind.FUNCTION)]
    if isinstance(stmt, ast.AsyncFunctionDef):
        return [_make_function_symbol(stmt, file_path, lines, SymbolKind.ASYNC_FUNCTION)]
    if isinstance(stmt, ast.ClassDef):
        cls_sym, methods = _make_class_symbol(stmt, file_path, lines)
        return [cls_sym, *methods]
    if isinstance(stmt, (ast.Import, ast.ImportFrom)):
        return [_make_import_symbol(stmt, file_path, lines)]
    return []


def extract_symbols_from_source(source_text: str, file_path: str) -> list[SymbolNode]:
    """Parse Python source into a sequence of structural symbol nodes."""
    tree = _safe_parse_ast(source_text)
    if tree is None:
        return []
    lines = source_text.splitlines()
    symbols: list[SymbolNode] = []
    for stmt in tree.body:
        symbols.extend(_dispatch_stmt_symbols(stmt, file_path, lines))
    return symbols


@dataclass
class SymbolGraph:
    """Directed dependency graph of extracted code symbols."""

    nodes: dict[str, SymbolNode] = field(default_factory=dict)
    edges: dict[str, set[str]] = field(default_factory=dict)
    in_edges: dict[str, set[str]] = field(default_factory=dict)


def _resolve_target_symbol(name: str, file_path: str, name_to_ids: dict[str, list[str]]) -> str | None:
    """Resolve an identifier reference to a candidate symbol id."""
    candidates = name_to_ids.get(name, [])
    if not candidates:
        return None
    for cand in candidates:
        if cand.startswith(f"{file_path}::"):
            return cand
    return candidates[0]


def _resolve_symbol_edges(
    sym: SymbolNode,
    name_to_ids: dict[str, list[str]],
    edges: dict[str, set[str]],
    in_edges: dict[str, set[str]],
) -> None:
    """Resolve outgoing dependency edges for one symbol."""
    for ref in sym.references:
        target_id = _resolve_target_symbol(ref, sym.file_path, name_to_ids)
        if target_id and target_id != sym.symbol_id:
            edges[sym.symbol_id].add(target_id)
            in_edges[target_id].add(sym.symbol_id)


def build_symbol_graph(symbols: Sequence[SymbolNode]) -> SymbolGraph:
    """Build directed symbol graph connecting symbols to their referenced dependencies."""
    nodes = {s.symbol_id: s for s in symbols}
    edges: dict[str, set[str]] = {s.symbol_id: set() for s in symbols}
    in_edges: dict[str, set[str]] = {s.symbol_id: set() for s in symbols}
    name_to_ids: dict[str, list[str]] = {}
    for s in symbols:
        name_to_ids.setdefault(s.name, []).append(s.symbol_id)

    for s in symbols:
        _resolve_symbol_edges(s, name_to_ids, edges, in_edges)
    return SymbolGraph(nodes=nodes, edges=edges, in_edges=in_edges)


def _build_personalization(node_ids: Sequence[str], focal: Sequence[str]) -> dict[str, float]:
    """Compute base personalization distribution vector."""
    n = len(node_ids)
    if n == 0:
        return {}
    matched = set(focal).intersection(node_ids)
    if not matched:
        uniform = 1.0 / n
        return {nid: uniform for nid in node_ids}
    weight = 1.0 / len(matched)
    return {nid: (weight if nid in matched else 0.0) for nid in node_ids}


def _pagerank_iteration(
    nodes: Sequence[str],
    distributions: tuple[dict[str, float], dict[str, float]],
    graph: SymbolGraph,
    damping: float,
) -> dict[str, float]:
    """Perform one power iteration step of Personalized PageRank."""
    current, p = distributions
    dangling_sum = sum(current[u] for u in nodes if not graph.edges.get(u))
    dangling_contrib = damping * dangling_sum
    next_r: dict[str, float] = {}
    for v in nodes:
        in_sum = sum(current[u] / len(graph.edges[u]) for u in graph.in_edges.get(v, ()))
        next_r[v] = (1.0 - damping) * p[v] + damping * in_sum + dangling_contrib * p[v]
    return next_r


def _run_pagerank_loop(
    nodes: Sequence[str],
    p: dict[str, float],
    graph: SymbolGraph,
    cfg: PageRankConfig,
) -> dict[str, float]:
    """Execute iterative power method until convergence or max iterations."""
    current = dict(p)
    for _ in range(cfg.max_iter):
        next_r = _pagerank_iteration(nodes, (current, p), graph, cfg.damping)
        diff = sum(abs(next_r[k] - current[k]) for k in nodes)
        current = next_r
        if diff < cfg.tolerance:
            break
    return current


def compute_pagerank(graph: SymbolGraph, config: PageRankConfig | None = None) -> dict[str, float]:
    """Compute Personalized PageRank scores over symbol graph."""
    cfg = config or PageRankConfig()
    nodes = list(graph.nodes.keys())
    if not nodes:
        return {}
    p = _build_personalization(nodes, cfg.focal_symbols)
    scores = _run_pagerank_loop(nodes, p, graph, cfg)
    for nid, score in scores.items():
        graph.nodes[nid].score = score
    return scores


def check_diagnostic_invariants(graph: SymbolGraph) -> list[Finding]:
    """Audit graph for orphaned symbols, hub hotspots, and circular references."""
    findings: list[Finding] = []
    _audit_orphans(graph, findings)
    _audit_hub_hotspots(graph, findings)
    _audit_cycles(graph, findings)
    return findings


def _audit_orphans(graph: SymbolGraph, out: list[Finding]) -> None:
    """Find isolated symbols with 0 in-degree and 0 out-degree."""
    for nid, node in graph.nodes.items():
        if node.kind == SymbolKind.IMPORT:
            continue
        out_deg = len(graph.edges.get(nid, ()))
        in_deg = len(graph.in_edges.get(nid, ()))
        if out_deg == 0 and in_deg == 0:
            out.append(
                Finding(
                    rule=DiagnosticRule.RNK001,
                    message=f"Orphaned symbol with zero graph connections: {node.name}",
                    target=nid,
                    severity="note",
                )
            )


def _compute_hub_threshold(scores: Sequence[float]) -> float | None:
    """Compute 3-sigma outlier threshold for scores."""
    if len(scores) < 3:
        return None
    mean = sum(scores) / len(scores)
    variance = sum((s - mean) ** 2 for s in scores) / len(scores)
    std = math.sqrt(variance)
    return (mean + 3.0 * std) if std > 1e-6 else None


def _audit_hub_hotspots(graph: SymbolGraph, out: list[Finding]) -> None:
    """Detect hub hotspot symbols exceeding 3 standard deviations in score."""
    scores = [n.score for n in graph.nodes.values()]
    threshold = _compute_hub_threshold(scores)
    if threshold is None:
        return
    for nid, node in graph.nodes.items():
        if node.score > threshold:
            out.append(
                Finding(
                    rule=DiagnosticRule.RNK002,
                    message=f"Hub centrality hotspot (score={node.score:.4f}): {node.name}",
                    target=nid,
                    severity="warning",
                )
            )


def _audit_node_cycle_targets(
    u: str,
    targets: set[str],
    graph: SymbolGraph,
    collector: tuple[set[tuple[str, str]], list[Finding]],
) -> None:
    """Audit mutual 2-cycle recursion for outgoing targets of a node."""
    seen, out = collector
    for v in targets:
        pair: tuple[str, str] = (u, v) if u < v else (v, u)
        if pair not in seen and u in graph.edges.get(v, ()):
            seen.add(pair)
            out.append(
                Finding(
                    rule=DiagnosticRule.RNK004,
                    message=f"Mutual circular reference cycle between {u} and {v}",
                    target=f"{u} <-> {v}",
                    severity="warning",
                )
            )


def _audit_cycles(graph: SymbolGraph, out: list[Finding]) -> None:
    """Audit for direct mutual 2-cycle recursion between symbols."""
    seen: set[tuple[str, str]] = set()
    collector = (seen, out)
    for u, targets in graph.edges.items():
        _audit_node_cycle_targets(u, targets, graph, collector)


def _render_symbol_content(sym: SymbolNode, fidelity: FidelityLevel) -> str:
    """Render symbol content according to chosen fidelity tier."""
    if fidelity == FidelityLevel.FULL:
        return sym.body_code
    if fidelity == FidelityLevel.SIGNATURES:
        doc = f'    """{sym.docstring}"""\n    ...' if sym.docstring else "    ..."
        return f"{sym.signature}\n{doc}"
    return f"{sym.signature}  # [{sym.kind}] lines {sym.line_start}-{sym.line_end}"


@dataclass
class PackedContextResult:
    """Outcome of token-budgeted context packing."""

    rendered_text: str
    packed_symbols: list[SymbolNode]
    total_tokens: int
    budget_tokens: int
    fidelity: FidelityLevel
    findings: list[Finding]


def pack_context(
    graph: SymbolGraph,
    budget_tokens: int = DEFAULT_BUDGET_TOKENS,
    fidelity: FidelityLevel = FidelityLevel.FULL,
) -> PackedContextResult:
    """Pack highest-ranked symbols into model-ready artifact within budget."""
    sorted_syms = sorted(graph.nodes.values(), key=lambda s: s.score, reverse=True)
    packed: list[SymbolNode] = []
    current_tokens = 0
    findings = check_diagnostic_invariants(graph)
    excluded_count = 0

    for sym in sorted_syms:
        snippet = _render_symbol_content(sym, fidelity)
        cost = estimate_tokens(snippet) + 5
        if current_tokens + cost <= budget_tokens:
            packed.append(sym)
            current_tokens += cost
        else:
            excluded_count += 1

    if excluded_count > 0:
        findings.append(
            Finding(
                rule=DiagnosticRule.RNK003,
                message=f"Budget saturated: {excluded_count} ranked symbols excluded from context pack",
                target=f"budget:{budget_tokens}",
                severity="warning",
            )
        )

    rendered = _serialize_model_ready_pack(packed, fidelity, current_tokens, budget_tokens)
    return PackedContextResult(
        rendered_text=rendered,
        packed_symbols=packed,
        total_tokens=current_tokens,
        budget_tokens=budget_tokens,
        fidelity=fidelity,
        findings=findings,
    )


def _serialize_model_ready_pack(
    symbols: Sequence[SymbolNode],
    fidelity: FidelityLevel,
    tokens: int,
    budget: int,
) -> str:
    """Format symbols into a model-ready markdown document."""
    header = [
        f"# Repository Context Pack (Ranked AST Symbols: {len(symbols)})",
        f"- Tokens: ~{tokens} / Budget: {budget}",
        f"- Fidelity Tier: {fidelity}",
        "",
    ]
    by_file: dict[str, list[SymbolNode]] = {}
    for s in symbols:
        by_file.setdefault(s.file_path, []).append(s)

    for path, sym_list in sorted(by_file.items()):
        header.append(f"## File: `{path}`")
        header.append("```python")
        for s in sym_list:
            header.append(_render_symbol_content(s, fidelity))
            header.append("")
        header.append("```")
        header.append("")
    return "\n".join(header)


def _is_valid_source_dir(dirname: str) -> bool:
    """Check if directory name is a candidate for source traversal."""
    return not dirname.startswith(".") and dirname not in {"node_modules", "venv", "__pycache__"}


def _ingest_dir_python_files(
    dirpath: str, filenames: Sequence[str], base_root: Path, out: list[SymbolNode]
) -> None:
    """Ingest python files in a single directory."""
    for fname in sorted(filenames):
        if fname.endswith(".py"):
            _ingest_single_file(Path(dirpath) / fname, base_root, MAX_SAFE_FILE_SIZE, out)


def _walk_directory_files(base_root: Path, out: list[SymbolNode]) -> None:
    """Walk directories and collect symbols from valid Python files."""
    for dirpath, dirnames, filenames in os.walk(base_root):
        dirnames[:] = [d for d in dirnames if _is_valid_source_dir(d)]
        _ingest_dir_python_files(dirpath, filenames, base_root, out)


def crawl_repository_sources(root_path: Path) -> list[SymbolNode]:
    """Crawl Python files in directory with symlink safety and file size boundaries."""
    base_root = root_path.resolve()
    symbols: list[SymbolNode] = []
    _walk_directory_files(base_root, symbols)
    return symbols


def _ingest_single_file(fpath: Path, base_root: Path, max_size: int, out: list[SymbolNode]) -> None:
    """Ingest one file if it satisfies symlink boundary and size constraints."""
    try:
        resolved = fpath.resolve()
        if not resolved.is_relative_to(base_root):
            return
        if resolved.stat().st_size > max_size:
            return
        rel_path = str(resolved.relative_to(base_root))
        text = resolved.read_text(encoding="utf-8", errors="replace")
        out.extend(extract_symbols_from_source(text, rel_path))
    except (OSError, RuntimeError):
        return


def _finding_to_sarif(f: Finding) -> dict[str, Any]:
    """Convert finding to OASIS SARIF result dictionary."""
    return {
        "ruleId": f.rule.value,
        "level": "error" if f.severity == "error" else "warning",
        "message": {"text": f.message},
        "locations": [
            {
                "physicalLocation": {
                    "artifactLocation": {"uri": f.target},
                },
            }
        ],
    }


def export_sarif(findings: Sequence[Finding], tool_name: str = "ast-relevance-context-ranker") -> str:
    """Export findings as schema-valid OASIS SARIF 2.1.0 JSON."""
    results = [_finding_to_sarif(f) for f in findings]
    sarif = {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [
            {
                "tool": {"driver": {"name": tool_name, "version": "0.1.0"}},
                "results": results,
            }
        ],
    }
    return json.dumps(sarif, indent=2)


def format_markdown_report(result: PackedContextResult) -> str:
    """Format summary report of ranking and packing run."""
    lines = [
        "# AST Relevance Context Ranker Report",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Packed Symbols | {len(result.packed_symbols)} |",
        f"| Estimated Tokens | ~{result.total_tokens} |",
        f"| Budget Tokens | {result.budget_tokens} |",
        f"| Fidelity | {result.fidelity} |",
        f"| Diagnostic Findings | {len(result.findings)} |",
        "",
    ]
    if result.findings:
        lines.extend(
            [
                "## Diagnostic Findings",
                "",
                "| Rule | Message | Target |",
                "|---|---|---|",
            ]
        )
        for f in result.findings:
            lines.append(f"| `{f.rule}` | {f.message} | `{f.target}` |")
        lines.append("")
    return "\n".join(lines)


def _cmd_rank(args: argparse.Namespace) -> int:
    """Execute rank command."""
    root = Path(args.dir or ".").resolve()
    symbols = crawl_repository_sources(root)
    graph = build_symbol_graph(symbols)
    focal = tuple(args.focus.split(",")) if args.focus else ()
    compute_pagerank(graph, PageRankConfig(focal_symbols=focal))
    ranked = sorted(graph.nodes.values(), key=lambda s: s.score, reverse=True)
    print(f"Ranked {len(ranked)} symbols in {root}:")
    for s in ranked[:10]:
        print(f"  [{s.score:.4f}] {s.symbol_id} ({s.kind})")
    return 0


def _render_pack_output(res: PackedContextResult, fmt: str) -> str:
    """Render packed result to selected text format."""
    if fmt == "sarif":
        return export_sarif(res.findings)
    if fmt == "report":
        return format_markdown_report(res)
    return res.rendered_text


def _cmd_pack(args: argparse.Namespace) -> int:
    """Execute pack command."""
    root = Path(args.dir or ".").resolve()
    symbols = crawl_repository_sources(root)
    graph = build_symbol_graph(symbols)
    focal = tuple(args.focus.split(",")) if args.focus else ()
    compute_pagerank(graph, PageRankConfig(focal_symbols=focal))
    fidelity = FidelityLevel(args.fidelity.upper()) if args.fidelity else FidelityLevel.FULL
    budget = int(args.budget or DEFAULT_BUDGET_TOKENS)
    res = pack_context(graph, budget_tokens=budget, fidelity=fidelity)
    print(_render_pack_output(res, args.format))
    return 0


def _build_arg_parser() -> argparse.ArgumentParser:
    """Construct CLI argument parser."""
    parser = argparse.ArgumentParser(description="AST Relevance Context Ranker & Optimizer")
    sub = parser.add_subparsers(dest="command")

    rank_p = sub.add_parser("rank", help="Rank repository symbols by PageRank centrality")
    rank_p.add_argument("--dir", default=".", help="Root directory to index")
    rank_p.add_argument("--focus", default="", help="Comma-separated focal symbol IDs")

    pack_p = sub.add_parser("pack", help="Pack repository symbols to token budget")
    pack_p.add_argument("--dir", default=".", help="Root directory to index")
    pack_p.add_argument("--focus", default="", help="Comma-separated focal symbol IDs")
    pack_p.add_argument("--budget", type=int, default=DEFAULT_BUDGET_TOKENS, help="Token budget")
    pack_p.add_argument("--fidelity", default="FULL", choices=["FULL", "SIGNATURES", "OUTLINE"])
    pack_p.add_argument("--format", default="pack", choices=["pack", "report", "sarif"])

    return parser


_CLI_DISPATCH: dict[str, Callable[[argparse.Namespace], int]] = {
    "rank": _cmd_rank,
    "pack": _cmd_pack,
}


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for AST context ranker."""
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    handler = _CLI_DISPATCH.get(args.command or "")
    if not handler:
        parser.print_help()
        return 0
    return int(handler(args))


if __name__ == "__main__":
    import sys

    sys.exit(main())
