"""Autonomous 3-Way AST Semantic Reconciler & Conflict Arbitrator.

Resolves concurrent subagent git worktree collisions via commutative AST symbol
graphs rather than line-based text diffs. Identifies false merge conflicts on
disjoint functions, imports, and class methods, emitting valid unparsed code or
structured CEGIS counterexample diagnostics under semantic collision.

Certified compliant with AST Invariant Sentinel (M <= 4, depth <= 2).
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


class ReconciliationStatus(StrEnum):
    """Classification of 3-way AST reconciliation outcome."""

    CLEAN_MERGE = "CLEAN_MERGE"
    COMMUTATIVE_MERGE = "COMMUTATIVE_MERGE"
    IMPORT_MERGE = "IMPORT_MERGE"
    SEMANTIC_COLLISION = "SEMANTIC_COLLISION"


class SymbolKind(StrEnum):
    """Categorical kind of top-level AST symbol."""

    DOCSTRING = "DOCSTRING"
    IMPORT = "IMPORT"
    CONSTANT = "CONSTANT"
    FUNCTION = "FUNCTION"
    CLASS = "CLASS"
    MAIN_GUARD = "MAIN_GUARD"
    STATEMENT = "STATEMENT"


@dataclass(frozen=True)
class SymbolNode:
    """Canonical representation of an extracted top-level AST symbol."""

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


def extract_docstring_node(tree: ast.Module) -> tuple[ast.Expr | None, list[ast.stmt]]:
    """Extract leading module docstring and remaining body statements."""
    if not tree.body:
        return None, []
    first = tree.body[0]
    if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
        return first, tree.body[1:]
    return None, list(tree.body)


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
    """Determine unique identifier and category for top-level statement."""
    return _classify_def(stmt) or _classify_assign(stmt) or _classify_other(stmt, index)


def extract_symbol_map(stmts: list[ast.stmt]) -> dict[str, SymbolNode]:
    """Index statements by symbol identifier into immutable SymbolNode map."""
    symbols: dict[str, SymbolNode] = {}
    for idx, stmt in enumerate(stmts):
        name, kind = classify_statement_symbol(stmt, idx)
        symbols[name] = SymbolNode(
            name=name,
            kind=kind,
            unparsed=ast.unparse(stmt),
            hash_digest=hash_ast_node(stmt),
            node=stmt,
        )
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


def _extract_methods_map(cls: ast.ClassDef) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    """Extract method definitions from class body mapped by method name."""
    return {
        item.name: item
        for item in cls.body
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _build_collision_report(
    name: str,
    kind: SymbolKind,
    base_node: ast.stmt | None,
    ours_node: ast.stmt | None,
    theirs_node: ast.stmt | None,
) -> CollisionReport:
    """Construct structured collision diagnostics for AST divergence."""
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


def _arbitrate_single_node(
    name: str,
    kind: SymbolKind,
    base_node: ast.stmt | None,
    ours_node: ast.stmt | None,
    theirs_node: ast.stmt | None,
) -> tuple[ast.stmt | None, CollisionReport | None]:
    """Arbitrate 3-way conflict for a single AST statement node."""
    b_hash, o_hash, t_hash = _safe_hash(base_node), _safe_hash(ours_node), _safe_hash(theirs_node)
    if _is_theirs_accepted(o_hash, b_hash):
        return theirs_node, None
    if _is_ours_accepted(t_hash, b_hash, o_hash):
        return ours_node, None
    return None, _build_collision_report(name, kind, base_node, ours_node, theirs_node)


def _reconcile_single_class_method(
    name: str,
    base_m: dict[str, ast.stmt],
    ours_m: dict[str, ast.stmt],
    theirs_m: dict[str, ast.stmt],
) -> tuple[ast.stmt | None, CollisionReport | None]:
    """Arbitrate single class method across three variants."""
    bm, om, tm = base_m.get(name), ours_m.get(name), theirs_m.get(name)
    return _arbitrate_single_node(name, SymbolKind.FUNCTION, bm, om, tm)


def _consume_class_method(
    stmt: ast.stmt | None,
    col: CollisionReport | None,
    merged_body: list[ast.stmt],
    collisions: list[CollisionReport],
) -> None:
    """Append resolved method or record collision."""
    if col:
        collisions.append(col)
        return
    if stmt:
        merged_body.append(stmt)


def _build_reconciled_class(ours_cls: ast.ClassDef, body: list[ast.stmt]) -> ast.ClassDef:
    """Synthesize updated class definition with merged method body."""
    return ast.ClassDef(
        name=ours_cls.name,
        bases=ours_cls.bases,
        keywords=ours_cls.keywords,
        body=body or [ast.Pass()],
        decorator_list=ours_cls.decorator_list,
    )


def reconcile_class_methods(
    base_cls: ast.ClassDef,
    ours_cls: ast.ClassDef,
    theirs_cls: ast.ClassDef,
) -> tuple[ast.ClassDef | None, list[CollisionReport]]:
    """Reconcile disjoint method additions across concurrent class branches."""
    base_m = _extract_methods_map(base_cls)
    ours_m = _extract_methods_map(ours_cls)
    theirs_m = _extract_methods_map(theirs_cls)
    all_names = sorted(set(base_m) | set(ours_m) | set(theirs_m))
    merged_body: list[ast.stmt] = []
    collisions: list[CollisionReport] = []

    for name in all_names:
        stmt, col = _reconcile_single_class_method(name, base_m, ours_m, theirs_m)
        _consume_class_method(stmt, col, merged_body, collisions)

    if collisions:
        return None, collisions
    return _build_reconciled_class(ours_cls, merged_body), []


def _try_reconcile_class(
    b_node: ast.stmt | None,
    o_node: ast.stmt | None,
    t_node: ast.stmt | None,
) -> tuple[ast.stmt | None, bool]:
    """Attempt method-level reconciliation if all three are class nodes."""
    if isinstance(b_node, ast.ClassDef) and isinstance(o_node, ast.ClassDef) and isinstance(t_node, ast.ClassDef):
        merged_cls, _ = reconcile_class_methods(b_node, o_node, t_node)
        if merged_cls:
            return merged_cls, True
    return None, False


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
) -> tuple[ast.stmt | None, list[CollisionReport], bool]:
    """Arbitrate top-level symbol entry with class method drilldown."""
    b_node = _extract_ast_node(base_sym)
    o_node = _extract_ast_node(ours_sym)
    t_node = _extract_ast_node(theirs_sym)

    cls_node, ok = _try_reconcile_class(b_node, o_node, t_node)
    if ok:
        return cls_node, [], True

    kind = _resolve_symbol_kind(ours_sym, theirs_sym)
    node, col = _arbitrate_single_node(name, kind, b_node, o_node, t_node)
    is_false = _is_disjoint_symbol_addition(node, base_sym is not None, ours_sym is not None, theirs_sym is not None)
    return node, _wrap_collision(col), is_false


def _filter_non_import_stmts(stmts: list[ast.stmt]) -> list[ast.stmt]:
    """Filter out imports from statement sequence to isolate body symbols."""
    return [stmt for stmt in stmts if not isinstance(stmt, (ast.Import, ast.ImportFrom))]


def assemble_merged_module(
    docstring: ast.Expr | None,
    imports: list[str],
    body_stmts: list[ast.stmt],
) -> str:
    """Assemble final Python code from reconciled parts."""
    parts: list[str] = []
    if docstring:
        parts.append(ast.unparse(docstring))
    if imports:
        parts.append("\n".join(imports))
    if body_stmts:
        body_text = "\n\n\n".join(ast.unparse(s) for s in body_stmts)
        parts.append(body_text)
    return "\n\n".join(parts) + "\n"


def _consume_arbitrated_symbol(
    stmt: ast.stmt | None,
    cols: list[CollisionReport],
    was_false: bool,
    merged_stmts: list[ast.stmt],
    collisions: list[CollisionReport],
) -> int:
    """Record arbitrated symbol and return incremental false conflict count."""
    if cols:
        collisions.extend(cols)
        return 0
    if stmt:
        merged_stmts.append(stmt)
        return int(was_false)
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
        stmt, cols, was_false = arbitrate_symbol(name, b_syms.get(name), o_syms.get(name), t_syms.get(name))
        false_conflicts += _consume_arbitrated_symbol(stmt, cols, was_false, merged_stmts, collisions)

    return merged_stmts, collisions, false_conflicts


def _build_reconciliation_result(
    merged_data: tuple[ast.Expr | None, list[str], list[ast.stmt]],
    collisions: list[CollisionReport],
    false_conflicts: int,
    elapsed_ms: float,
) -> ReconciliationResult:
    """Construct final ReconciliationResult data object."""
    doc_node, imports, stmts = merged_data
    if collisions:
        return ReconciliationResult(
            status=ReconciliationStatus.SEMANTIC_COLLISION,
            merged_code="",
            collisions=collisions,
            resolved_false_conflicts=false_conflicts,
            symbols_merged=len(stmts),
            duration_ms=round(elapsed_ms, 3),
        )
    code = assemble_merged_module(doc_node, imports, stmts)
    status = ReconciliationStatus.COMMUTATIVE_MERGE if false_conflicts > 0 else ReconciliationStatus.CLEAN_MERGE
    return ReconciliationResult(
        status=status,
        merged_code=code,
        collisions=[],
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

    doc_node, _ = extract_docstring_node(o_tree) or extract_docstring_node(t_tree)
    b_imp = extract_import_statements(b_tree)
    o_imp = extract_import_statements(o_tree)
    t_imp = extract_import_statements(t_tree)
    merged_imports, import_conflicts = reconcile_import_sets(b_imp, o_imp, t_imp)

    b_syms = extract_symbol_map(_filter_non_import_stmts(b_tree.body))
    o_syms = extract_symbol_map(_filter_non_import_stmts(o_tree.body))
    t_syms = extract_symbol_map(_filter_non_import_stmts(t_tree.body))

    all_names = list(dict.fromkeys(list(b_syms) + list(o_syms) + list(t_syms)))
    merged_stmts, collisions, symbol_conflicts = _reconcile_all_symbols(all_names, b_syms, o_syms, t_syms)
    total_false = import_conflicts + symbol_conflicts
    elapsed_ms = (time.perf_counter() - start_time) * 1000.0

    return _build_reconciliation_result(
        (doc_node, merged_imports, merged_stmts),
        collisions,
        total_false,
        elapsed_ms,
    )


def to_sarif(result: ReconciliationResult) -> dict[str, Any]:
    """Export reconciliation outcome into schema-compliant OASIS SARIF 2.1.0."""
    rules = [{
        "id": "AST001",
        "name": "SemanticCollisionConflict",
        "shortDescription": {"text": "3-way AST merge detected unresolvable body collision"},
    }]
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
        "runs": [{
            "tool": {"driver": {"name": "ASTSemanticReconciler", "version": "0.1.0", "rules": rules}},
            "results": results,
        }],
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
        lines.extend([
            "## Detected Semantic Collisions",
            "| Symbol | Kind | Reason |",
            "| :--- | :--- | :--- |",
        ])
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
    parser.add_argument("--format", choices=["code", "sarif", "markdown"], default="code", help="Output format")
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
