"""Graph-Ranked AST Context Optimizer & Relevance Packer.

Extracts abstract syntax tree (AST) symbol graphs from repository source trees,
computes Personalized PageRank centrality over symbol dependencies and call graphs,
and packs the highest-ranked whole symbols into an estimated token budget.

Passes the AST Invariant Sentinel `strict` preset (M <= 6, depth <= 3).
"""

from __future__ import annotations

import argparse
import ast
import copy
import json
import math
import os
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

DEFAULT_DAMPING: Final[float] = 0.85
DEFAULT_TOLERANCE: Final[float] = 1e-5
# Each power step shrinks the L1 change by at least the damping factor, and the first change
# is at most 2. At d = 0.85 the change is below 1e-5 by step 77 on any graph, so 100 steps
# (also the NetworkX default) always converge with the default damping and tolerance.
DEFAULT_MAX_ITER: Final[int] = 100
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
    parent_id: str | None = None  # The class of a method, else None


class PageRankConvergenceError(RuntimeError):
    """Power iteration used `max_iter` steps and the L1 change did not fall below `tolerance`."""


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


def _statement_source(source_text: str, node: ast.stmt) -> str:
    """Return the exact source of one statement, moved to column 0 so that it parses alone.

    `ast.get_source_segment` cuts at the statement's own start and end, with the line
    breaks that the tokenizer uses; `str.splitlines` also breaks on a form feed. The
    later lines of a method keep the class indentation, so remove the indentation of
    the first line from each line that starts with it.
    """
    segment = ast.get_source_segment(source_text, node, padded=True) or ""
    lines = segment.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    indent = lines[0][: len(lines[0]) - len(lines[0].lstrip())]
    return "\n".join(line.removeprefix(indent) for line in lines)


def _declaration_header(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> str:
    """Render the `def` or `class` header from the AST, so that it is always complete.

    A source slice kept only the first line of a multi-line header and kept a body
    that shares the header line. `ast.unparse` writes the whole header on one line.
    """
    stub = copy.copy(node)
    stub.decorator_list = []
    stub.body = [ast.Expr(ast.Constant(...))]
    return ast.unparse(stub).rsplit("\n", 1)[0]


def _make_function_symbol(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    file_path: str,
    source_text: str,
    kind: SymbolKind,
) -> SymbolNode:
    """Construct a SymbolNode for a function or method."""
    end_line = getattr(node, "end_lineno", node.lineno)
    body = _statement_source(source_text, node)
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
        signature=_declaration_header(node),
        body_code=body,
        tokens=estimate_tokens(body),
        references=sorted(vis.names),
    )


def _make_class_symbol(
    node: ast.ClassDef,
    file_path: str,
    source_text: str,
) -> tuple[SymbolNode, list[SymbolNode]]:
    """Construct class SymbolNode and nested member methods."""
    end_line = getattr(node, "end_lineno", node.lineno)
    body = _statement_source(source_text, node)
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
        signature=_declaration_header(node),
        body_code=body,
        tokens=estimate_tokens(body),
        references=sorted(vis.names),
    )
    methods = _extract_class_methods(node, file_path, source_text)
    return class_sym, methods


def _extract_class_methods(
    cls_node: ast.ClassDef,
    file_path: str,
    source_text: str,
) -> list[SymbolNode]:
    """Extract nested methods from a class definition."""
    methods: list[SymbolNode] = []
    for item in cls_node.body:
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            m_sym = _make_function_symbol(item, file_path, source_text, SymbolKind.METHOD)
            m_sym.symbol_id = f"{file_path}::{cls_node.name}.{item.name}"
            m_sym.name = f"{cls_node.name}.{item.name}"
            m_sym.parent_id = f"{file_path}::{cls_node.name}"
            methods.append(m_sym)
    return methods


def _imported_name(node: ast.Import | ast.ImportFrom, alias: ast.alias) -> str:
    """Name one imported name as `module` or `module:member`, with ` as <alias>` if it has one.

    The `:` keeps `import x.y` apart from `from x import y`, and the alias keeps
    `from x import y` apart from `from x import y as z`.
    """
    name = alias.name
    if isinstance(node, ast.ImportFrom):
        name = f"{'.' * node.level}{node.module or ''}:{alias.name}"
    return f"{name} as {alias.asname}" if alias.asname else name


def _make_import_symbols(node: ast.Import | ast.ImportFrom, file_path: str) -> list[SymbolNode]:
    """Construct one SymbolNode for each name that an import statement imports.

    The id holds the line and the imported name, so that two imports on one line stay two
    symbols. The body is a one-name statement written with `ast.unparse`, so that a pack
    does not repeat `import a, b` for each name.
    """
    symbols: list[SymbolNode] = []
    for alias in node.names:
        stub = copy.copy(node)
        stub.names = [alias]
        body = ast.unparse(stub)
        line_name = f"import_{node.lineno}_{_imported_name(node, alias)}"
        symbols.append(
            SymbolNode(
                symbol_id=f"{file_path}::{line_name}",
                name=line_name,
                file_path=file_path,
                kind=SymbolKind.IMPORT,
                line_start=node.lineno,
                line_end=getattr(node, "end_lineno", node.lineno),
                docstring=None,
                signature=body,
                body_code=body,
                tokens=estimate_tokens(body),
                references=[alias.name],
            )
        )
    return symbols


def _safe_parse_ast(source_text: str) -> ast.Module | None:
    """Safely parse python source into an AST module."""
    try:
        return ast.parse(source_text)
    except SyntaxError:
        return None


def _dispatch_stmt_symbols(
    stmt: ast.stmt,
    file_path: str,
    source_text: str,
) -> list[SymbolNode]:
    """Convert an AST statement into its constituent symbol nodes."""
    if isinstance(stmt, ast.FunctionDef):
        return [_make_function_symbol(stmt, file_path, source_text, SymbolKind.FUNCTION)]
    if isinstance(stmt, ast.AsyncFunctionDef):
        return [_make_function_symbol(stmt, file_path, source_text, SymbolKind.ASYNC_FUNCTION)]
    if isinstance(stmt, ast.ClassDef):
        cls_sym, methods = _make_class_symbol(stmt, file_path, source_text)
        return [cls_sym, *methods]
    if isinstance(stmt, (ast.Import, ast.ImportFrom)):
        return _make_import_symbols(stmt, file_path)
    return []


def extract_symbols_from_source(source_text: str, file_path: str) -> list[SymbolNode]:
    """Parse Python source into a sequence of structural symbol nodes."""
    tree = _safe_parse_ast(source_text)
    if tree is None:
        return []
    symbols: list[SymbolNode] = []
    for stmt in tree.body:
        symbols.extend(_dispatch_stmt_symbols(stmt, file_path, source_text))
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
    """Run the power method until the L1 change falls below the tolerance.

    Raises PageRankConvergenceError when `max_iter` steps pass first: scores that are
    still moving are not a ranking.
    """
    current = dict(p)
    diff = math.inf
    for _ in range(cfg.max_iter):
        next_r = _pagerank_iteration(nodes, (current, p), graph, cfg.damping)
        diff = sum(abs(next_r[k] - current[k]) for k in nodes)
        current = next_r
        if diff < cfg.tolerance:
            return current
    raise PageRankConvergenceError(
        f"PageRank did not converge in {cfg.max_iter} steps: "
        f"the last L1 change {diff:.3g} is not below the tolerance {cfg.tolerance:g}"
    )


def compute_pagerank(graph: SymbolGraph, config: PageRankConfig | None = None) -> dict[str, float]:
    """Compute Personalized PageRank scores over symbol graph.

    Raises PageRankConvergenceError when the iteration does not converge in `max_iter` steps.
    """
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
    """Render one symbol as a complete top-level statement at the chosen fidelity tier.

    An import is a whole statement at every tier. A `def` or `class` header gets the body
    `...`, and at SIGNATURES its docstring first, written with `repr` so that a quote in
    the docstring cannot close the literal early.
    """
    if fidelity == FidelityLevel.OUTLINE:
        return _render_outline(sym)
    if fidelity == FidelityLevel.FULL or sym.kind == SymbolKind.IMPORT:
        return sym.body_code
    doc = f"    {sym.docstring!r}\n" if sym.docstring else ""
    return f"{sym.signature}\n{doc}    ..."


def _render_outline(sym: SymbolNode) -> str:
    """Render the OUTLINE tier: the header or import, a kind and line comment, and `...`."""
    marker = f"  # [{sym.kind}] lines {sym.line_start}-{sym.line_end}"
    if sym.kind == SymbolKind.IMPORT:
        return f"{sym.body_code}{marker}"
    return f"{sym.signature}{marker}\n    ..."


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
    findings = check_diagnostic_invariants(graph)
    packed, current_tokens, excluded_count = _pack_greedily(sorted_syms, budget_tokens, fidelity)

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


def _pack_greedily(
    ranked: Sequence[SymbolNode],
    budget: int,
    fidelity: FidelityLevel,
) -> tuple[list[SymbolNode], int, int]:
    """Pack symbols in rank order while they fit. Return the pack, its tokens and the excluded count.

    At FULL, the source of a class holds its methods. A method whose class is packed is
    skipped, and a class packed after some of its methods replaces them, so that no
    method body is in the pack twice.
    """
    packed: dict[str, tuple[SymbolNode, int]] = {}
    used = 0
    excluded = 0
    for sym in ranked:
        if fidelity == FidelityLevel.FULL and sym.parent_id in packed:
            continue
        cost = estimate_tokens(_render_symbol_content(sym, fidelity)) + 5
        if used + cost > budget:
            excluded += 1
            continue
        packed[sym.symbol_id] = (sym, cost)
        used += cost - _drop_packed_methods(packed, sym, fidelity)
    return [s for s, _ in packed.values()], used, excluded


def _drop_packed_methods(
    packed: dict[str, tuple[SymbolNode, int]],
    sym: SymbolNode,
    fidelity: FidelityLevel,
) -> int:
    """At FULL, remove the methods of a class that was just packed. Return their tokens."""
    if fidelity != FidelityLevel.FULL or sym.kind != SymbolKind.CLASS:
        return 0
    methods = [nid for nid, (member, _) in packed.items() if member.parent_id == sym.symbol_id]
    return sum(packed.pop(nid)[1] for nid in methods)


def _code_fence(code: str) -> str:
    """Return a backtick fence one longer than the longest backtick run in the code, at least 3.

    CommonMark 0.31.2 §4.5 closes a fenced block at the first line of backticks that is at
    least as long as the opening fence, so a line of three backticks in a string closed a
    fence of three early.
    """
    longest = max((len(run) for run in re.findall(r"`+", code)), default=0)
    return "`" * max(3, longest + 1)


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
        code = "\n".join(f"{_render_symbol_content(s, fidelity)}\n" for s in sym_list)
        fence = _code_fence(code)
        header.extend([f"## File: `{path}`", f"{fence}python", code, fence, ""])
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
