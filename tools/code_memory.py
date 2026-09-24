"""Semantic Graph AST Code Memory & Persistent Symbol Indexing.

Maintains call graph invariants, def-use chains, signature stability,
and blast-radius boundaries across multi-file refactoring sessions.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import sys
from collections import deque
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

DEFAULT_INDEX_FILE: Final[str] = ".data/code_memory_index.json"
MAX_MERMAID_NODES: Final[int] = 20
HIGH_BLAST_RADIUS_THRESHOLD: Final[int] = 5


class SymbolKind(StrEnum):
    """Categorical classification of indexed code entities."""

    MODULE = "module"
    CLASS = "class"
    FUNCTION = "function"
    METHOD = "method"
    VARIABLE = "variable"


class EdgeKind(StrEnum):
    """Relational dependency between indexed code entities."""

    CALLS = "calls"
    IMPORTS = "imports"
    INHERITS = "inherits"
    DEFINES = "defines"


@dataclass(frozen=True)
class SymbolSignature:
    """Formal parameter signature of callable code entities."""

    params: list[str]
    required_count: int
    total_count: int
    has_varargs: bool
    has_varkwargs: bool
    kwonly_args: list[str]


@dataclass(frozen=True)
class SymbolNode:
    """Indexed code entity node in the semantic graph."""

    id: str
    name: str
    kind: SymbolKind
    file_path: str
    line: int
    signature: SymbolSignature | None
    docstring: str | None
    content_hash: str


@dataclass(frozen=True)
class GraphEdge:
    """Directed dependency edge between two symbols."""

    source_id: str
    target_id: str
    kind: EdgeKind
    call_site_line: int
    call_arg_count: int | None = None


@dataclass(frozen=True)
class CodeMemoryFinding:
    """Audit finding emitted when call graph invariants are breached."""

    rule_id: str
    severity: str
    message: str
    file_path: str
    line: int
    symbol_id: str | None = None


def compute_sha256(content: str) -> str:
    """Compute hexadecimal SHA-256 digest of input text."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def extract_function_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> SymbolSignature:
    """Extract arity and parameter constraints from an AST function definition."""
    args = node.args
    pos_args = [a.arg for a in args.posonlyargs] + [a.arg for a in args.args]
    total_pos = len(pos_args)
    default_count = len(args.defaults)
    required_count = max(0, total_pos - default_count)
    has_varargs = args.vararg is not None
    has_varkw = args.kwarg is not None
    kwonly = [a.arg for a in args.kwonlyargs]

    return SymbolSignature(
        params=pos_args,
        required_count=required_count,
        total_count=total_pos,
        has_varargs=has_varargs,
        has_varkwargs=has_varkw,
        kwonly_args=kwonly,
    )


def extract_call_name(node: ast.Call) -> str | None:
    """Resolve identifier name or attribute name from an AST call node."""
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


class SymbolASTVisitor(ast.NodeVisitor):
    """Traverse an AST to extract symbol definitions and dependency edges."""

    def __init__(self, file_path: str, code: str) -> None:
        """Initialize the visitor with source file context."""
        self.file_path = file_path
        self.code = code
        self.module_id = file_path
        self.scope_stack: list[str] = [self.module_id]
        self.symbols: list[SymbolNode] = []
        self.edges: list[GraphEdge] = []
        self.imports_map: dict[str, str] = {}

    def current_scope(self) -> str:
        """Return the current qualified enclosing symbol identifier."""
        return self.scope_stack[-1]

    def visit_Import(self, node: ast.Import) -> None:
        """Record imported modules and create import edges."""
        for alias in node.names:
            alias_name = alias.asname or alias.name
            self.imports_map[alias_name] = alias.name
            self.edges.append(
                GraphEdge(
                    source_id=self.module_id,
                    target_id=alias.name,
                    kind=EdgeKind.IMPORTS,
                    call_site_line=node.lineno,
                )
            )
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        """Record imported symbols from target module."""
        module_name = node.module or ""
        for alias in node.names:
            alias_name = alias.asname or alias.name
            target_id = f"{module_name}.{alias.name}" if module_name else alias.name
            self.imports_map[alias_name] = target_id
            self.edges.append(
                GraphEdge(
                    source_id=self.module_id,
                    target_id=target_id,
                    kind=EdgeKind.IMPORTS,
                    call_site_line=node.lineno,
                )
            )
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        """Extract class definition node and inheritance edges."""
        class_id = f"{self.file_path}::{node.name}"
        doc = ast.get_docstring(node)
        node_hash = compute_sha256(f"class:{node.name}:{node.lineno}")

        self.symbols.append(
            SymbolNode(
                id=class_id,
                name=node.name,
                kind=SymbolKind.CLASS,
                file_path=self.file_path,
                line=node.lineno,
                signature=None,
                docstring=doc,
                content_hash=node_hash,
            )
        )
        self.edges.append(
            GraphEdge(
                source_id=self.current_scope(),
                target_id=class_id,
                kind=EdgeKind.DEFINES,
                call_site_line=node.lineno,
            )
        )

        for base in node.bases:
            base_name = getattr(base, "id", None) or getattr(base, "attr", None)
            if base_name:
                resolved_base = self.imports_map.get(base_name, base_name)
                self.edges.append(
                    GraphEdge(
                        source_id=class_id,
                        target_id=resolved_base,
                        kind=EdgeKind.INHERITS,
                        call_site_line=node.lineno,
                    )
                )

        self.scope_stack.append(class_id)
        self.generic_visit(node)
        self.scope_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        """Extract function or method definition and signature."""
        self._record_function(node, is_async=False)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        """Extract async function or method definition and signature."""
        self._record_function(node, is_async=True)

    def _record_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef, is_async: bool) -> None:
        """Record function symbol and traverse child statements."""
        is_method = len(self.scope_stack) > 1 and "::" in self.scope_stack[-1]
        kind = SymbolKind.METHOD if is_method else SymbolKind.FUNCTION
        func_id = f"{self.scope_stack[-1]}.{node.name}" if is_method else f"{self.file_path}::{node.name}"
        doc = ast.get_docstring(node)
        sig = extract_function_signature(node)
        node_hash = compute_sha256(
            f"{'async_' if is_async else ''}func:{node.name}:{sig.params}:{node.lineno}"
        )

        self.symbols.append(
            SymbolNode(
                id=func_id,
                name=node.name,
                kind=kind,
                file_path=self.file_path,
                line=node.lineno,
                signature=sig,
                docstring=doc,
                content_hash=node_hash,
            )
        )
        self.edges.append(
            GraphEdge(
                source_id=self.current_scope(),
                target_id=func_id,
                kind=EdgeKind.DEFINES,
                call_site_line=node.lineno,
            )
        )

        self.scope_stack.append(func_id)
        self.generic_visit(node)
        self.scope_stack.pop()

    def visit_Call(self, node: ast.Call) -> None:
        """Extract call invocation edges and argument count."""
        name = extract_call_name(node)
        if name:
            resolved_target = self.imports_map.get(name, name)
            self.edges.append(
                GraphEdge(
                    source_id=self.current_scope(),
                    target_id=resolved_target,
                    kind=EdgeKind.CALLS,
                    call_site_line=node.lineno,
                    call_arg_count=len(node.args),
                )
            )
        self.generic_visit(node)


class CodeMemoryIndex:
    """Persistent semantic code graph maintaining symbol registry and call graph."""

    def __init__(self) -> None:
        """Initialize empty graph structures."""
        self.symbols: dict[str, SymbolNode] = {}
        self.edges: list[GraphEdge] = []
        self.file_hashes: dict[str, str] = {}
        self.file_symbols: dict[str, list[str]] = {}
        self.forward_edges: dict[str, list[GraphEdge]] = {}
        self.backward_edges: dict[str, list[GraphEdge]] = {}

    def index_file(self, file_path: str, code: str) -> bool:
        """Parse source code into AST symbols and update the graph if changed."""
        cur_hash = compute_sha256(code)
        if self.file_hashes.get(file_path) == cur_hash:
            return False

        self._remove_file_symbols(file_path)
        try:
            tree = ast.parse(code, filename=file_path)
        except SyntaxError:
            return False

        visitor = SymbolASTVisitor(file_path, code)
        visitor.visit(tree)

        self._add_file_artifacts(file_path, cur_hash, visitor.symbols, visitor.edges)
        return True

    def _remove_file_symbols(self, file_path: str) -> None:
        """Remove previously indexed symbols and edges for a given file."""
        old_symbol_ids = set(self.file_symbols.get(file_path, []))
        for sid in old_symbol_ids:
            self.symbols.pop(sid, None)

        self.edges = [
            e
            for e in self.edges
            if e.source_id not in old_symbol_ids
            and e.target_id not in old_symbol_ids
            and not e.source_id.startswith(f"{file_path}::")
        ]
        self._rebuild_adjacency()

    def _add_file_artifacts(
        self,
        file_path: str,
        content_hash: str,
        new_symbols: list[SymbolNode],
        new_edges: list[GraphEdge],
    ) -> None:
        """Register newly extracted symbols and dependency edges."""
        self.file_hashes[file_path] = content_hash
        self.file_symbols[file_path] = [s.id for s in new_symbols]
        for s in new_symbols:
            self.symbols[s.id] = s
        self.edges.extend(new_edges)
        self._rebuild_adjacency()

    def _rebuild_adjacency(self) -> None:
        """Rebuild forward and backward edge lookup tables."""
        self.forward_edges.clear()
        self.backward_edges.clear()
        for edge in self.edges:
            self.forward_edges.setdefault(edge.source_id, []).append(edge)
            self.backward_edges.setdefault(edge.target_id, []).append(edge)

    def _step_blast_radius(self, current: str, visited: set[str], queue: deque[str]) -> None:
        """Process incoming edges for current symbol in blast radius BFS."""
        for edge in self.backward_edges.get(current, []):
            if edge.kind == EdgeKind.DEFINES:
                continue
            src = edge.source_id
            if src not in visited:
                visited.add(src)
                queue.append(src)

    def calculate_blast_radius(self, symbol_id: str) -> set[str]:
        """Compute transitive upstream closure of symbols affected by modifying target."""
        visited: set[str] = set()
        queue: deque[str] = deque([symbol_id])

        while queue:
            self._step_blast_radius(queue.popleft(), visited, queue)

        visited.discard(symbol_id)
        return visited

    def detect_dependency_cycles(self) -> list[list[str]]:
        """Identify directed cycles among indexed modules and functions."""
        visited: dict[str, int] = {}
        path: list[str] = []
        cycles: list[list[str]] = []

        nodes = list(self.symbols.keys())
        for node in nodes:
            if visited.get(node, 0) == 0:
                self._dfs_cycle(node, visited, path, cycles)

        return cycles

    def _step_cycle_edge(
        self,
        edge: GraphEdge,
        visited: dict[str, int],
        path: list[str],
        cycles: list[list[str]],
    ) -> None:
        """Evaluate a single dependency edge in DFS cycle traversal."""
        nxt = edge.target_id
        state = visited.get(nxt, 0)
        if state == 1:
            idx = path.index(nxt)
            cycles.append([*path[idx:], nxt])
        elif state == 0 and len(cycles) < 10:
            self._dfs_cycle(nxt, visited, path, cycles)

    def _dfs_cycle(
        self,
        current: str,
        visited: dict[str, int],
        path: list[str],
        cycles: list[list[str]],
    ) -> None:
        """DFS recursive step detecting back-edges in dependency graph."""
        visited[current] = 1
        path.append(current)

        for edge in self.forward_edges.get(current, []):
            self._step_cycle_edge(edge, visited, path, cycles)

        path.pop()
        visited[current] = 2

    def to_dict(self) -> dict[str, Any]:
        """Serialize index into dictionary representation."""
        return {
            "symbols": {k: asdict(v) for k, v in self.symbols.items()},
            "edges": [asdict(e) for e in self.edges],
            "file_hashes": self.file_hashes,
            "file_symbols": self.file_symbols,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CodeMemoryIndex:
        """Deserialize index from dictionary representation."""
        idx = cls()
        idx.file_hashes = data.get("file_hashes", {})
        idx.file_symbols = data.get("file_symbols", {})

        for k, v in data.get("symbols", {}).items():
            sig_data = v.get("signature")
            sig = SymbolSignature(**sig_data) if sig_data else None
            idx.symbols[k] = SymbolNode(
                id=v["id"],
                name=v["name"],
                kind=SymbolKind(v["kind"]),
                file_path=v["file_path"],
                line=v["line"],
                signature=sig,
                docstring=v.get("docstring"),
                content_hash=v["content_hash"],
            )

        for e in data.get("edges", []):
            idx.edges.append(
                GraphEdge(
                    source_id=e["source_id"],
                    target_id=e["target_id"],
                    kind=EdgeKind(e["kind"]),
                    call_site_line=e["call_site_line"],
                    call_arg_count=e.get("call_arg_count"),
                )
            )

        idx._rebuild_adjacency()
        return idx


class CodeMemoryOracle:
    """Verifies refactoring operations against call graph invariants."""

    def __init__(self, index: CodeMemoryIndex) -> None:
        """Initialize oracle with indexed semantic code graph."""
        self.index = index

    def audit_all(self, modified_symbols: set[str] | None = None) -> list[CodeMemoryFinding]:
        """Execute full suite of call graph invariant audits."""
        findings: list[CodeMemoryFinding] = []
        findings.extend(self.audit_dangling_references())
        findings.extend(self.audit_signature_mismatches())
        findings.extend(self.audit_dependency_cycles())
        findings.extend(self.audit_orphaned_definitions())
        if modified_symbols:
            findings.extend(self.audit_high_blast_radius(modified_symbols))
        return findings

    def _check_dangling_edge(self, edge: GraphEdge, known_ids: set[str]) -> CodeMemoryFinding | None:
        """Evaluate whether a graph edge targets an unindexed symbol."""
        if edge.kind not in (EdgeKind.CALLS, EdgeKind.IMPORTS):
            return None
        target = edge.target_id
        if "::" not in target or target in known_ids:
            return None
        src_sym = self.index.symbols.get(edge.source_id)
        fpath = src_sym.file_path if src_sym else "unknown"
        return CodeMemoryFinding(
            rule_id="SGM001",
            severity="error",
            message=f"Dangling reference to unindexed symbol '{target}'.",
            file_path=fpath,
            line=edge.call_site_line,
            symbol_id=edge.source_id,
        )

    def audit_dangling_references(self) -> list[CodeMemoryFinding]:
        """Detect invocations targeting removed or unresolvable symbols (SGM001)."""
        findings: list[CodeMemoryFinding] = []
        known_ids = set(self.index.symbols.keys())
        for edge in self.index.edges:
            finding = self._check_dangling_edge(edge, known_ids)
            if finding:
                findings.append(finding)
        return findings

    def _check_sig_mismatch(self, edge: GraphEdge, target_sym: SymbolNode) -> CodeMemoryFinding | None:
        """Verify call arguments against signature constraints."""
        sig = target_sym.signature
        if not sig or edge.call_arg_count is None:
            return None
        passed = edge.call_arg_count
        if passed < sig.required_count:
            return self._create_sig_finding(
                edge,
                target_sym,
                f"Call passes {passed} args; '{target_sym.name}' requires {sig.required_count}.",
            )
        if not sig.has_varargs and passed > sig.total_count:
            return self._create_sig_finding(
                edge,
                target_sym,
                f"Call passes {passed} args; '{target_sym.name}' accepts at most {sig.total_count}.",
            )
        return None

    def _evaluate_sig_edge(self, edge: GraphEdge) -> CodeMemoryFinding | None:
        """Audit a single call edge for signature conformance."""
        if edge.kind != EdgeKind.CALLS:
            return None
        target_sym = self._resolve_target_symbol(edge.target_id)
        return self._check_sig_mismatch(edge, target_sym) if target_sym else None

    def audit_signature_mismatches(self) -> list[CodeMemoryFinding]:
        """Detect call sites passing invalid positional argument counts (SGM002)."""
        findings: list[CodeMemoryFinding] = []
        for edge in self.index.edges:
            finding = self._evaluate_sig_edge(edge)
            if finding:
                findings.append(finding)
        return findings

    def _resolve_target_symbol(self, target_id: str) -> SymbolNode | None:
        """Resolve a target identifier to an indexed SymbolNode."""
        if target_id in self.index.symbols:
            return self.index.symbols[target_id]
        matching = [s for s in self.index.symbols.values() if s.name == target_id]
        return matching[0] if len(matching) == 1 else None

    def _create_sig_finding(self, edge: GraphEdge, target: SymbolNode, msg: str) -> CodeMemoryFinding:
        """Synthesize a signature mismatch finding."""
        src_sym = self.index.symbols.get(edge.source_id)
        fpath = src_sym.file_path if src_sym else "unknown"
        return CodeMemoryFinding(
            rule_id="SGM002",
            severity="error",
            message=msg,
            file_path=fpath,
            line=edge.call_site_line,
            symbol_id=edge.source_id,
        )

    def audit_dependency_cycles(self) -> list[CodeMemoryFinding]:
        """Detect circular call or import dependency cycles (SGM003)."""
        findings: list[CodeMemoryFinding] = []
        cycles = self.index.detect_dependency_cycles()

        for cycle in cycles:
            chain = " -> ".join(s.split("::")[-1] for s in cycle)
            head = cycle[0]
            sym = self.index.symbols.get(head)
            fpath = sym.file_path if sym else "unknown"
            line = sym.line if sym else 1
            findings.append(
                CodeMemoryFinding(
                    rule_id="SGM003",
                    severity="error",
                    message=f"Circular dependency cycle detected: {chain}",
                    file_path=fpath,
                    line=line,
                    symbol_id=head,
                )
            )

        return findings

    def audit_orphaned_definitions(self) -> list[CodeMemoryFinding]:
        """Detect unreachable symbol definitions lacking incoming references (SGM004)."""
        findings: list[CodeMemoryFinding] = []

        for sid, sym in self.index.symbols.items():
            if self._is_exempt_orphan(sym):
                continue
            in_edges = self.index.backward_edges.get(sid, [])
            calls_in = [e for e in in_edges if e.kind in (EdgeKind.CALLS, EdgeKind.INHERITS)]
            if not calls_in:
                findings.append(
                    CodeMemoryFinding(
                        rule_id="SGM004",
                        severity="warning",
                        message=f"Orphaned symbol '{sym.name}' has 0 incoming references.",
                        file_path=sym.file_path,
                        line=sym.line,
                        symbol_id=sid,
                    )
                )

        return findings

    def _is_exempt_orphan(self, sym: SymbolNode) -> bool:
        """Check if symbol is exempt from dead code orphan warnings."""
        if sym.name.startswith("test_") or sym.name == "main":
            return True
        if sym.name.startswith("__") and sym.name.endswith("__"):
            return True
        return sym.kind == SymbolKind.MODULE

    def audit_high_blast_radius(
        self,
        modified_symbols: set[str],
        threshold: int = HIGH_BLAST_RADIUS_THRESHOLD,
    ) -> list[CodeMemoryFinding]:
        """Warn when refactoring symbols with large transitive impact (SGM005)."""
        findings: list[CodeMemoryFinding] = []

        for sid in modified_symbols:
            sym = self.index.symbols.get(sid)
            if not sym:
                continue
            radius = self.index.calculate_blast_radius(sid)
            if len(radius) >= threshold:
                findings.append(
                    CodeMemoryFinding(
                        rule_id="SGM005",
                        severity="warning",
                        message=f"High blast radius refactor on '{sym.name}': impacts {len(radius)} upstream callers.",
                        file_path=sym.file_path,
                        line=sym.line,
                        symbol_id=sid,
                    )
                )

        return findings


def generate_sarif(findings: list[CodeMemoryFinding]) -> dict[str, Any]:
    """Export code memory findings to OASIS SARIF 2.1.0 format."""
    results = []
    for f in findings:
        level = "error" if f.severity == "error" else "warning"
        results.append(
            {
                "ruleId": f.rule_id,
                "level": level,
                "message": {"text": f.message},
                "locations": [
                    {
                        "physicalLocation": {
                            "artifactLocation": {"uri": f.file_path},
                            "region": {"startLine": f.line},
                        }
                    }
                ],
            }
        )

    return {
        "$schema": "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/master/Schemata/sarif-schema-2.1.0.json",
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "vibes-code-memory",
                        "informationUri": "https://github.com/dan-petty/vibes",
                        "rules": [
                            {
                                "id": "SGM001",
                                "name": "DanglingSymbolReference",
                                "shortDescription": {
                                    "text": "Invocation targets unindexed or removed symbol."
                                },
                            },
                            {
                                "id": "SGM002",
                                "name": "SignatureMismatchRegression",
                                "shortDescription": {"text": "Call site passes mismatched parameter arity."},
                            },
                            {
                                "id": "SGM003",
                                "name": "CyclicDependencyInduction",
                                "shortDescription": {"text": "Circular dependency cycle detected in graph."},
                            },
                            {
                                "id": "SGM004",
                                "name": "OrphanedDefinition",
                                "shortDescription": {
                                    "text": "Symbol definition has zero incoming references."
                                },
                            },
                            {
                                "id": "SGM005",
                                "name": "HighBlastRadiusUnverifiedRefactor",
                                "shortDescription": {
                                    "text": "Symbol refactoring impacts extensive call hierarchy."
                                },
                            },
                        ],
                    }
                },
                "results": results,
            }
        ],
    }


def _render_mermaid_nodes(nodes: list[SymbolNode], lines: list[str]) -> None:
    """Render node declarations in Mermaid graph."""
    for n in nodes:
        lbl = n.name.replace('"', '\\"')
        lines.append(f'    {_safe_mermaid_id(n.id)}["{lbl}<br>({n.kind.value})"]')


def _render_mermaid_edges(edges: list[GraphEdge], node_ids: set[str], lines: list[str]) -> None:
    """Render edge connections in Mermaid graph."""
    for edge in edges:
        if edge.source_id in node_ids and edge.target_id in node_ids:
            src = _safe_mermaid_id(edge.source_id)
            tgt = _safe_mermaid_id(edge.target_id)
            lines.append(f"    {src} -->|{edge.kind.value}| {tgt}")


def _render_mermaid_styles(nodes: list[SymbolNode], lines: list[str]) -> None:
    """Render WCAG AA compliant styles for Mermaid graph."""
    for n in nodes:
        mid = _safe_mermaid_id(n.id)
        fill = "#1e3a8a" if n.kind == SymbolKind.CLASS else "#064e3b"
        stroke = "#38bdf8" if n.kind == SymbolKind.CLASS else "#10b981"
        lines.append(f"    style {mid} fill:{fill},stroke:{stroke},stroke-width:2px,color:#ffffff")


def generate_mermaid_graph(index: CodeMemoryIndex, max_nodes: int = MAX_MERMAID_NODES) -> str:
    """Render semantic code graph as a WCAG AA compliant Mermaid flowchart."""
    lines = ["flowchart TD"]
    nodes = list(index.symbols.values())[:max_nodes]
    node_ids = {n.id for n in nodes}

    _render_mermaid_nodes(nodes, lines)
    _render_mermaid_edges(index.edges, node_ids, lines)
    _render_mermaid_styles(nodes, lines)

    return "\n".join(lines)


def _safe_mermaid_id(identifier: str) -> str:
    """Sanitize arbitrary symbol identifier for Mermaid node ID syntax."""
    return identifier.replace("/", "_").replace(".", "_").replace("::", "_").replace("-", "_")


def generate_markdown_report(findings: list[CodeMemoryFinding], index: CodeMemoryIndex) -> str:
    """Synthesize Markdown summary report of code memory index and findings."""
    lines = [
        "# Semantic Graph AST Code Memory Audit Report",
        "",
        "## Summary Metrics",
        f"- **Total Indexed Symbols**: {len(index.symbols)}",
        f"- **Total Dependency Edges**: {len(index.edges)}",
        f"- **Indexed Source Files**: {len(index.file_hashes)}",
        f"- **Invariant Violations Identified**: {len(findings)}",
        "",
        "## Diagnostic Findings",
        "",
        "| Rule ID | Severity | File | Line | Finding Message |",
        "|---|---|---|---|---|",
    ]

    for f in findings:
        lines.append(f"| `{f.rule_id}` | `{f.severity}` | `{f.file_path}` | {f.line} | {f.message} |")

    return "\n".join(lines)


def _index_single_file(idx: CodeMemoryIndex, root: Path, py_file: Path) -> None:
    """Index an individual Python file if accessible."""
    if ".venv" in py_file.parts or ".git" in py_file.parts:
        return
    try:
        content = py_file.read_text(encoding="utf-8")
        rel_path = str(py_file.relative_to(root))
        idx.index_file(rel_path, content)
    except (OSError, UnicodeDecodeError):
        pass


def index_directory(directory: str | Path, index: CodeMemoryIndex | None = None) -> CodeMemoryIndex:
    """Recursively index all Python source files in target directory."""
    idx = index or CodeMemoryIndex()
    root = Path(directory).resolve()
    for py_file in root.rglob("*.py"):
        _index_single_file(idx, root, py_file)
    return idx


def _handle_cli_outputs(
    args: argparse.Namespace, index: CodeMemoryIndex, findings: list[CodeMemoryFinding]
) -> None:
    """Dispatch optional file exports from CLI invocation."""
    if args.blast_radius:
        affected = index.calculate_blast_radius(args.blast_radius)
        print(f"Blast Radius for {args.blast_radius}: {len(affected)} affected symbols.")
    if args.sarif:
        Path(args.sarif).write_text(json.dumps(generate_sarif(findings), indent=2), encoding="utf-8")
    if args.mermaid:
        Path(args.mermaid).write_text(generate_mermaid_graph(index), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for Semantic Graph AST Code Memory & Persistent Symbol Indexing."""
    parser = argparse.ArgumentParser(description="Semantic Graph AST Code Memory and Symbol Indexer.")
    parser.add_argument("--index", help="Directory of source code to index.")
    parser.add_argument("--verify", action="store_true", help="Execute all invariant audit gates.")
    parser.add_argument("--sarif", help="Path to write SARIF 2.1.0 output report.")
    parser.add_argument("--mermaid", help="Path to write Mermaid flowchart output.")
    parser.add_argument("--blast-radius", help="Query blast radius for a given symbol identifier.")

    args = parser.parse_args(argv)
    index = CodeMemoryIndex()
    if args.index:
        index_directory(args.index, index)

    oracle = CodeMemoryOracle(index)
    findings = oracle.audit_all() if args.verify else []
    _handle_cli_outputs(args, index, findings)

    return 1 if any(f.severity == "error" for f in findings) else 0


if __name__ == "__main__":
    sys.exit(main())
