"""Autonomous 3-Way AST Semantic Reconciler & Conflict Arbitrator.

Resolves concurrent subagent git worktree collisions via commutative AST symbol
graphs rather than line-based text diffs. Identifies false merge conflicts on
disjoint functions, imports, and class members, emitting unparsed code (comments
are lost) or structured collision diagnostics under semantic collision. Merged
code that does not parse again is reported as a collision, never returned.

Passes the AST Invariant Sentinel `strict` preset (M <= 6, depth <= 3).
"""

from __future__ import annotations

import argparse
import ast
import copy
import hashlib
import json
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Any

# Keys that no Python identifier can take, so they never clash with a symbol name.
DOCSTRING_KEY = "<docstring>"
MODULE_KEY = "<module>"


class ReconciliationStatus(StrEnum):
    """Classification of 3-way AST reconciliation outcome."""

    CLEAN_MERGE = "CLEAN_MERGE"
    COMMUTATIVE_MERGE = "COMMUTATIVE_MERGE"
    IMPORT_MERGE = "IMPORT_MERGE"
    SEMANTIC_COLLISION = "SEMANTIC_COLLISION"


class SymbolKind(StrEnum):
    """Categorical kind of a module or class body symbol."""

    DOCSTRING = "DOCSTRING"
    IMPORT = "IMPORT"
    CONSTANT = "CONSTANT"
    FUNCTION = "FUNCTION"
    CLASS = "CLASS"
    MAIN_GUARD = "MAIN_GUARD"
    STATEMENT = "STATEMENT"
    MODULE = "MODULE"


@dataclass(frozen=True)
class SymbolNode:
    """Canonical representation of an extracted module or class body symbol."""

    name: str
    kind: SymbolKind
    unparsed: str
    hash_digest: str
    node: ast.stmt


@dataclass
class CollisionReport:
    """Diagnostic detail of an irreconcilable semantic AST conflict."""

    symbol_name: str
    symbol_kind: SymbolKind
    base_snippet: str
    ours_snippet: str
    theirs_snippet: str
    reason: str


@dataclass
class ReconciliationResult:
    """Telemetry and synthesized artifact from 3-way AST reconciliation."""

    status: ReconciliationStatus
    merged_code: str
    collisions: list[CollisionReport] = field(default_factory=list)
    resolved_false_conflicts: int = 0
    symbols_merged: int = 0
    duration_ms: float = 0.0


@dataclass
class BodyMerge:
    """Arbitrated docstring and statements of one module or class body."""

    docstring: ast.stmt | None
    statements: list[ast.stmt]
    collisions: list[CollisionReport]
    false_conflicts: int

    def full_body(self) -> list[ast.stmt]:
        """Return the docstring, if there is one, followed by the other statements."""
        if self.docstring is None:
            return list(self.statements)
        return [self.docstring, *self.statements]


SymbolMaps = tuple[dict[str, SymbolNode], dict[str, SymbolNode], dict[str, SymbolNode]]
ClassTriple = tuple[ast.ClassDef, ast.ClassDef, ast.ClassDef]
ArbitratedSymbol = tuple[ast.stmt | None, list[CollisionReport], int]


def hash_ast_node(node: ast.AST) -> str:
    """Compute SHA-256 fingerprint over normalized AST structure."""
    dumped = ast.dump(node, annotate_fields=False, include_attributes=False)
    return hashlib.sha256(dumped.encode("utf-8")).hexdigest()[:16]


def _safe_hash(node: ast.AST | None) -> str | None:
    """Compute hash if node is present, returning None otherwise."""
    return hash_ast_node(node) if node is not None else None


def parse_ast_safely(source: str) -> ast.Module:
    """Parse Python source into AST Module with bounded syntax handling."""
    try:
        return ast.parse(source)
    except SyntaxError as exc:
        raise ValueError(f"Syntax error during AST parse: {exc.msg} at line {exc.lineno}") from exc


def _is_docstring(stmt: ast.stmt) -> bool:
    """Return True if the statement is a bare string literal."""
    return (
        isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Constant)
        and isinstance(stmt.value.value, str)
    )


def extract_docstring_node(owner: ast.Module | ast.ClassDef) -> tuple[ast.Expr | None, list[ast.stmt]]:
    """Extract the leading docstring of a module or class and its remaining body statements."""
    first = owner.body[0] if owner.body else None
    if isinstance(first, ast.Expr) and _is_docstring(first):
        return first, owner.body[1:]
    return None, list(owner.body)


def _get_target_name(target: ast.AST) -> str:
    """Extract string identifier from assignment target."""
    return target.id if isinstance(target, ast.Name) else f"__expr_{hash_ast_node(target)}"


def _classify_def(stmt: ast.stmt) -> tuple[str, SymbolKind] | None:
    """Classify function or class definition statements."""
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return stmt.name, SymbolKind.FUNCTION
    if isinstance(stmt, ast.ClassDef):
        return stmt.name, SymbolKind.CLASS
    return None


def _classify_assign(stmt: ast.stmt) -> tuple[str, SymbolKind] | None:
    """Classify variable assignment statements."""
    if isinstance(stmt, ast.Assign) and stmt.targets:
        return _get_target_name(stmt.targets[0]), SymbolKind.CONSTANT
    if isinstance(stmt, ast.AnnAssign):
        return _get_target_name(stmt.target), SymbolKind.CONSTANT
    return None


def _classify_other(stmt: ast.stmt, idx: int) -> tuple[str, SymbolKind]:
    """Classify imports, execution guards, and generic statements."""
    if isinstance(stmt, (ast.Import, ast.ImportFrom)):
        return f"__import_{idx}__", SymbolKind.IMPORT
    if isinstance(stmt, ast.If) and ast.unparse(stmt.test) == "__name__ == '__main__'":
        return "__main__", SymbolKind.MAIN_GUARD
    return f"__stmt_{idx}__", SymbolKind.STATEMENT


def classify_statement_symbol(stmt: ast.stmt, index: int) -> tuple[str, SymbolKind]:
    """Determine unique identifier and category for a module or class body statement."""
    return _classify_def(stmt) or _classify_assign(stmt) or _classify_other(stmt, index)


def _make_symbol(name: str, kind: SymbolKind, stmt: ast.stmt) -> SymbolNode:
    """Wrap one statement in an immutable SymbolNode with its fingerprint."""
    return SymbolNode(
        name=name,
        kind=kind,
        unparsed=ast.unparse(stmt),
        hash_digest=hash_ast_node(stmt),
        node=stmt,
    )


def _unique_key(name: str, taken: dict[str, SymbolNode]) -> str:
    """Return the name, or `name#N` for the Nth statement with that name (a property setter)."""
    key, count = name, 1
    while key in taken:
        count += 1
        key = f"{name}#{count}"
    return key


def extract_symbol_map(stmts: list[ast.stmt]) -> dict[str, SymbolNode]:
    """Index statements by symbol identifier into immutable SymbolNode map."""
    symbols: dict[str, SymbolNode] = {}
    for idx, stmt in enumerate(stmts):
        name, kind = classify_statement_symbol(stmt, idx)
        key = _unique_key(name, symbols)
        symbols[key] = _make_symbol(key, kind, stmt)
    return symbols


def index_body(owner: ast.Module | ast.ClassDef, drop_imports: bool = False) -> dict[str, SymbolNode]:
    """Index a module or class body by symbol, with its docstring under DOCSTRING_KEY."""
    doc, rest = extract_docstring_node(owner)
    symbols = extract_symbol_map(_filter_non_import_stmts(rest) if drop_imports else rest)
    if doc is not None:
        symbols[DOCSTRING_KEY] = _make_symbol(DOCSTRING_KEY, SymbolKind.DOCSTRING, doc)
    return symbols


def extract_import_statements(tree: ast.Module) -> set[str]:
    """Extract all individual import lines as normalized unparsed strings."""
    imports: set[str] = set()
    for stmt in tree.body:
        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            imports.add(ast.unparse(stmt))
    return imports


def reconcile_import_sets(
    base_imports: set[str],
    ours_imports: set[str],
    theirs_imports: set[str],
) -> tuple[list[str], int]:
    """Reconcile 3-way import sets preserving additions and deduplicating."""
    retained_base = base_imports & ours_imports & theirs_imports
    ours_added = ours_imports - base_imports
    theirs_added = theirs_imports - base_imports
    total_imports = retained_base | ours_added | theirs_added
    false_conflicts = len(ours_added & theirs_added) + min(len(ours_added), len(theirs_added))
    return sorted(total_imports), false_conflicts


def _build_collision_report(
    name: str,
    kind: SymbolKind,
    nodes: tuple[ast.stmt | None, ast.stmt | None, ast.stmt | None],
) -> CollisionReport:
    """Construct structured collision diagnostics for AST divergence."""
    base_node, ours_node, theirs_node = nodes
    return CollisionReport(
        symbol_name=name,
        symbol_kind=kind,
        base_snippet=ast.unparse(base_node) if base_node else "<absent>",
        ours_snippet=ast.unparse(ours_node) if ours_node else "<absent>",
        theirs_snippet=ast.unparse(theirs_node) if theirs_node else "<absent>",
        reason=f"Concurrent conflicting modification to {kind.value} '{name}'",
    )


def _is_theirs_accepted(o_hash: str | None, b_hash: str | None) -> bool:
    """Return True if ours is unmodified relative to base."""
    return o_hash == b_hash


def _is_ours_accepted(t_hash: str | None, b_hash: str | None, o_hash: str | None) -> bool:
    """Return True if theirs is unmodified relative to base or both identical."""
    if t_hash == b_hash:
        return True
    return o_hash == t_hash


def _pick_variant(hashes: tuple[str | None, str | None, str | None]) -> int | None:
    """Return the index (1 ours, 2 theirs) of the variant a 3-way merge accepts, or None."""
    b_hash, o_hash, t_hash = hashes
    if _is_theirs_accepted(o_hash, b_hash):
        return 2
    if _is_ours_accepted(t_hash, b_hash, o_hash):
        return 1
    return None


def _arbitrate_single_node(
    name: str,
    kind: SymbolKind,
    nodes: tuple[ast.stmt | None, ast.stmt | None, ast.stmt | None],
) -> tuple[ast.stmt | None, CollisionReport | None]:
    """Arbitrate 3-way conflict for a single AST statement node."""
    base_node, ours_node, theirs_node = nodes
    index = _pick_variant((_safe_hash(base_node), _safe_hash(ours_node), _safe_hash(theirs_node)))
    if index is None:
        return None, _build_collision_report(name, kind, nodes)
    return nodes[index], None


def _as_class_triple(nodes: tuple[ast.stmt | None, ast.stmt | None, ast.stmt | None]) -> ClassTriple | None:
    """Return the three nodes as class definitions, or None if any is not a class."""
    base_node, ours_node, theirs_node = nodes
    if (
        isinstance(base_node, ast.ClassDef)
        and isinstance(ours_node, ast.ClassDef)
        and isinstance(theirs_node, ast.ClassDef)
    ):
        return base_node, ours_node, theirs_node
    return None


def _with_body(cls: ast.ClassDef, body: list[ast.stmt]) -> ast.ClassDef:
    """Return a shallow copy of a class definition with another body."""
    clone = copy.copy(cls)
    clone.body = body
    return clone


def _header_hash(cls: ast.ClassDef) -> str:
    """Hash the decorators, name, bases and keywords of a class without its body."""
    return hash_ast_node(_with_body(cls, []))


def _ordered_names(
    first: dict[str, SymbolNode],
    second: dict[str, SymbolNode],
    third: dict[str, SymbolNode],
) -> list[str]:
    """List the symbol names of three maps once each in first-seen order, without the docstring."""
    names = dict.fromkeys([*first, *second, *third])
    names.pop(DOCSTRING_KEY, None)
    return list(names)


def reconcile_class_members(name: str, classes: ClassTriple) -> ArbitratedSymbol:
    """Merge a class that both branches changed member by member, keeping every statement kind."""
    base_cls, ours_cls, theirs_cls = classes
    header_index = _pick_variant((_header_hash(base_cls), _header_hash(ours_cls), _header_hash(theirs_cls)))
    if header_index is None:
        return None, [_build_collision_report(name, SymbolKind.CLASS, classes)], 0
    maps = (index_body(base_cls), index_body(ours_cls), index_body(theirs_cls))
    merge = reconcile_bodies(maps, _ordered_names(maps[1], maps[2], maps[0]))
    if merge.collisions:
        return None, [replace(col, symbol_name=f"{name}.{col.symbol_name}") for col in merge.collisions], 0
    merged_cls = _with_body(classes[header_index], merge.full_body() or [ast.Pass()])
    return merged_cls, [], 1 + merge.false_conflicts


def _extract_ast_node(sym: SymbolNode | None) -> ast.stmt | None:
    """Safely extract AST statement from SymbolNode wrapper."""
    return sym.node if sym is not None else None


def _resolve_symbol_kind(ours_sym: SymbolNode | None, theirs_sym: SymbolNode | None) -> SymbolKind:
    """Identify categorical kind prioritising ours then theirs."""
    if ours_sym is not None:
        return ours_sym.kind
    if theirs_sym is not None:
        return theirs_sym.kind
    return SymbolKind.STATEMENT


def _is_disjoint_symbol_addition(
    node: ast.stmt | None,
    has_base: bool,
    has_ours: bool,
    has_theirs: bool,
) -> bool:
    """Determine if a resolved node represents a commutative disjoint addition."""
    if node is None or has_base:
        return False
    return has_ours != has_theirs


def _wrap_collision(col: CollisionReport | None) -> list[CollisionReport]:
    """Wrap collision in singleton list or return empty list."""
    return [col] if col is not None else []


def arbitrate_symbol(
    name: str,
    base_sym: SymbolNode | None,
    ours_sym: SymbolNode | None,
    theirs_sym: SymbolNode | None,
) -> ArbitratedSymbol:
    """Arbitrate one symbol; merge a class member by member when both branches changed it."""
    nodes = (_extract_ast_node(base_sym), _extract_ast_node(ours_sym), _extract_ast_node(theirs_sym))
    node, col = _arbitrate_single_node(name, _resolve_symbol_kind(ours_sym, theirs_sym), nodes)
    classes = _as_class_triple(nodes)
    if col is not None and classes is not None:
        return reconcile_class_members(name, classes)
    is_false = _is_disjoint_symbol_addition(
        node, base_sym is not None, ours_sym is not None, theirs_sym is not None
    )
    return node, _wrap_collision(col), int(is_false)


def _filter_non_import_stmts(stmts: list[ast.stmt]) -> list[ast.stmt]:
    """Filter out imports from statement sequence to isolate body symbols."""
    return [stmt for stmt in stmts if not isinstance(stmt, (ast.Import, ast.ImportFrom))]


def assemble_merged_module(
    docstring: ast.stmt | None,
    imports: list[str],
    body_stmts: list[ast.stmt],
) -> str:
    """Assemble final Python code from reconciled parts, with the docstring written once."""
    parts: list[str] = []
    if docstring is not None:
        parts.append(ast.unparse(ast.Module(body=[docstring], type_ignores=[])))
    if imports:
        parts.append("\n".join(imports))
    if body_stmts:
        body_text = "\n\n\n".join(ast.unparse(s) for s in body_stmts)
        parts.append(body_text)
    return "\n\n".join(parts) + "\n"


def _consume_arbitrated_symbol(
    result: ArbitratedSymbol,
    merged_stmts: list[ast.stmt],
    collisions: list[CollisionReport],
) -> int:
    """Record arbitrated symbol and return incremental false conflict count."""
    stmt, cols, false_count = result
    if cols:
        collisions.extend(cols)
        return 0
    if stmt is not None:
        merged_stmts.append(stmt)
        return false_count
    return 0


def _reconcile_all_symbols(
    names: list[str],
    b_syms: dict[str, SymbolNode],
    o_syms: dict[str, SymbolNode],
    t_syms: dict[str, SymbolNode],
) -> tuple[list[ast.stmt], list[CollisionReport], int]:
    """Iterate through distinct symbol identifiers and arbitrate each."""
    merged_stmts: list[ast.stmt] = []
    collisions: list[CollisionReport] = []
    false_conflicts = 0

    for name in names:
        res = arbitrate_symbol(name, b_syms.get(name), o_syms.get(name), t_syms.get(name))
        false_conflicts += _consume_arbitrated_symbol(res, merged_stmts, collisions)

    return merged_stmts, collisions, false_conflicts


def reconcile_bodies(maps: SymbolMaps, names: list[str]) -> BodyMerge:
    """Arbitrate the docstring of one module or class body, then its other symbols in order."""
    base, ours, theirs = maps
    doc_result = arbitrate_symbol(
        DOCSTRING_KEY, base.get(DOCSTRING_KEY), ours.get(DOCSTRING_KEY), theirs.get(DOCSTRING_KEY)
    )
    stmts, collisions, false_conflicts = _reconcile_all_symbols(names, base, ours, theirs)
    doc_node, doc_collisions, doc_false = doc_result
    return BodyMerge(doc_node, stmts, doc_collisions + collisions, doc_false + false_conflicts)


def _parse_failure(code: str) -> list[CollisionReport]:
    """Return one collision if the merged code does not parse, else no collision."""
    try:
        ast.parse(code)
    except SyntaxError as exc:
        return [
            CollisionReport(
                symbol_name=MODULE_KEY,
                symbol_kind=SymbolKind.MODULE,
                base_snippet="<n/a>",
                ours_snippet="<n/a>",
                theirs_snippet="<n/a>",
                reason=f"Merged code does not parse: {exc.msg} at line {exc.lineno}",
            )
        ]
    return []


def _classify_status(collisions: list[CollisionReport], false_conflicts: int) -> ReconciliationStatus:
    """Map collisions and resolved false conflicts to a reconciliation status."""
    if collisions:
        return ReconciliationStatus.SEMANTIC_COLLISION
    if false_conflicts > 0:
        return ReconciliationStatus.COMMUTATIVE_MERGE
    return ReconciliationStatus.CLEAN_MERGE


def _build_reconciliation_result(
    merged_data: tuple[ast.stmt | None, list[str], list[ast.stmt]],
    collisions: list[CollisionReport],
    false_conflicts: int,
    elapsed_ms: float,
) -> ReconciliationResult:
    """Assemble and re-parse the merged code, then construct the ReconciliationResult."""
    doc_node, imports, stmts = merged_data
    code = "" if collisions else assemble_merged_module(doc_node, imports, stmts)
    collisions = collisions or _parse_failure(code)
    return ReconciliationResult(
        status=_classify_status(collisions, false_conflicts),
        merged_code="" if collisions else code,
        collisions=collisions,
        resolved_false_conflicts=false_conflicts,
        symbols_merged=len(stmts),
        duration_ms=round(elapsed_ms, 3),
    )


def reconcile_3way(base_code: str, ours_code: str, theirs_code: str) -> ReconciliationResult:
    """Coordinate full 3-way AST semantic reconciliation across three variants."""
    start_time = time.perf_counter()
    b_tree = parse_ast_safely(base_code)
    o_tree = parse_ast_safely(ours_code)
    t_tree = parse_ast_safely(theirs_code)

    b_imp = extract_import_statements(b_tree)
    o_imp = extract_import_statements(o_tree)
    t_imp = extract_import_statements(t_tree)
    merged_imports, import_conflicts = reconcile_import_sets(b_imp, o_imp, t_imp)

    maps = (
        index_body(b_tree, drop_imports=True),
        index_body(o_tree, drop_imports=True),
        index_body(t_tree, drop_imports=True),
    )
    body = reconcile_bodies(maps, _ordered_names(*maps))
    total_false = import_conflicts + body.false_conflicts
    elapsed_ms = (time.perf_counter() - start_time) * 1000.0

    return _build_reconciliation_result(
        (body.docstring, merged_imports, body.statements),
        body.collisions,
        total_false,
        elapsed_ms,
    )


def to_sarif(result: ReconciliationResult) -> dict[str, Any]:
    """Export reconciliation outcome into schema-compliant OASIS SARIF 2.1.0."""
    rules = [
        {
            "id": "AST001",
            "name": "SemanticCollisionConflict",
            "shortDescription": {"text": "3-way AST merge detected unresolvable body collision"},
        }
    ]
    results = [
        {
            "ruleId": "AST001",
            "level": "error",
            "message": {"text": f"Conflict in {c.symbol_kind.value} '{c.symbol_name}': {c.reason}"},
            "locations": [{"physicalLocation": {"artifactLocation": {"uri": f"symbol://{c.symbol_name}"}}}],
        }
        for c in result.collisions
    ]
    return {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [
            {
                "tool": {"driver": {"name": "ASTSemanticReconciler", "version": "0.1.0", "rules": rules}},
                "results": results,
            }
        ],
    }


def to_markdown(result: ReconciliationResult) -> str:
    """Format human-readable Markdown reconciliation summary table."""
    badge = "✅ CLEAN" if not result.collisions else "❌ COLLISION"
    lines = [
        "# 3-Way AST Semantic Reconciler Report",
        "",
        f"**Status**: {result.status.value} ({badge})",
        f"- **Symbols Merged**: {result.symbols_merged}",
        f"- **Resolved False Conflicts**: {result.resolved_false_conflicts}",
        f"- **Duration**: {result.duration_ms} ms",
        "",
    ]
    if result.collisions:
        lines.extend(
            [
                "## Detected Semantic Collisions",
                "| Symbol | Kind | Reason |",
                "| :--- | :--- | :--- |",
            ]
        )
        for c in result.collisions:
            lines.append(f"| `{c.symbol_name}` | {c.symbol_kind.value} | {c.reason} |")
    return "\n".join(lines)


def build_cli_parser() -> argparse.ArgumentParser:
    """Build command line argument parser for git merge driver integration."""
    parser = argparse.ArgumentParser(
        description="Autonomous 3-Way AST Semantic Reconciler & Conflict Arbitrator",
    )
    parser.add_argument("--base", required=True, type=Path, help="Base common ancestor file")
    parser.add_argument("--ours", required=True, type=Path, help="Local/ours branch file")
    parser.add_argument("--theirs", required=True, type=Path, help="Remote/theirs branch file")
    parser.add_argument("--output", "-o", type=Path, help="Destination path for reconciled code")
    parser.add_argument(
        "--format", choices=["code", "sarif", "markdown"], default="code", help="Output format"
    )
    return parser


def _emit_sarif(res: ReconciliationResult, _out_path: Path | None) -> None:
    """Output SARIF JSON payload to standard output."""
    sys.stdout.write(json.dumps(to_sarif(res), indent=2) + "\n")


def _emit_markdown(res: ReconciliationResult, _out_path: Path | None) -> None:
    """Output Markdown summary table to standard output."""
    sys.stdout.write(to_markdown(res) + "\n")


def _emit_code(res: ReconciliationResult, out_path: Path | None) -> None:
    """Output unparsed python source to destination path or stdout."""
    if not res.merged_code:
        return
    if out_path:
        out_path.write_text(res.merged_code, encoding="utf-8")
    else:
        sys.stdout.write(res.merged_code)


_EMITTERS: dict[str, Callable[[ReconciliationResult, Path | None], None]] = {
    "sarif": _emit_sarif,
    "markdown": _emit_markdown,
    "code": _emit_code,
}


def _emit_output(res: ReconciliationResult, fmt: str, out_path: Path | None) -> None:
    """Route reconciliation result using dictionary dispatch."""
    emitter = _EMITTERS.get(fmt, _emit_code)
    emitter(res, out_path)


def main(argv: list[str] | None = None) -> int:
    """CLI driver compatible with git custom merge-driver protocol."""
    args = build_cli_parser().parse_args(argv)
    base_src = args.base.read_text(encoding="utf-8")
    ours_src = args.ours.read_text(encoding="utf-8")
    theirs_src = args.theirs.read_text(encoding="utf-8")

    res = reconcile_3way(base_src, ours_src, theirs_src)
    _emit_output(res, args.format, args.output)
    return 0 if not res.collisions else 1


if __name__ == "__main__":
    sys.exit(main())
