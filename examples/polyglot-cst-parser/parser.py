#!/usr/bin/env python3
"""Unified Polyglot CST Ingestion Engine & Complexity Auditor.

Extracts symbols (functions, methods, classes, structs, traits, interfaces),
computes language-agnostic cyclomatic complexity (M) and nesting depth, and
enforces defensive file size and symlink containment boundary guards across:
- Python
- Rust
- Go
- TypeScript / JavaScript
- Bash

Hardening Guarantees:
- Pre-flight file size capping (MAX_FILE_SIZE_BYTES <= 5MB) preventing OOM/DoS (CWE-400).
- Defensive symlink resolution trapping circular loops (ELOOP) and workspace escapes.
- Zero external wheel dependencies; standard-library execution with sub-second latency.
"""

from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass, field
from enum import Enum
import os
from pathlib import Path
import re
import sys
from typing import Sequence

MAX_FILE_SIZE_BYTES = 5 * 1024 * 1024  # 5MB boundary guard

EXT_TO_LANG: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".rs": "rust",
    ".go": "go",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".sh": "bash",
    ".bash": "bash",
}

# Regex patterns for polyglot symbol extraction
RUST_STRUCT_RE = re.compile(r"^\s*(?:pub(?:\([^)]+\))?\s+)?struct\s+([A-Za-z0-9_]+)", re.MULTILINE)
RUST_TRAIT_RE = re.compile(r"^\s*(?:pub(?:\([^)]+\))?\s+)?trait\s+([A-Za-z0-9_]+)", re.MULTILINE)
RUST_FN_RE = re.compile(r"^\s*(?:pub(?:\([^)]+\))?\s+)?(?:async\s+)?fn\s+([A-Za-z0-9_]+)", re.MULTILINE)

GO_TYPE_STRUCT_RE = re.compile(r"^\s*type\s+([A-Za-z0-9_]+)\s+struct\b", re.MULTILINE)
GO_TYPE_INTERFACE_RE = re.compile(r"^\s*type\s+([A-Za-z0-9_]+)\s+interface\b", re.MULTILINE)
GO_FUNC_RE = re.compile(r"^\s*func\s+(?:\([^)]+\)\s+)?([A-Za-z0-9_]+)\s*\(", re.MULTILINE)

TS_CLASS_RE = re.compile(r"^\s*(?:export\s+)?class\s+([A-Za-z0-9_]+)", re.MULTILINE)
TS_INTERFACE_RE = re.compile(r"^\s*(?:export\s+)?interface\s+([A-Za-z0-9_]+)", re.MULTILINE)
TS_FUNC_RE = re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z0-9_]+)", re.MULTILINE)

BASH_FUNC_RE = re.compile(r"^\s*(?:function\s+)?([A-Za-z0-9_-]+)\s*\(\)\s*\{", re.MULTILINE)


class SymbolKind(str, Enum):
    """Classification of extracted source code symbols."""

    FUNCTION = "FUNCTION"
    METHOD = "METHOD"
    CLASS = "CLASS"
    STRUCT = "STRUCT"
    INTERFACE = "INTERFACE"
    TRAIT = "TRAIT"


@dataclass
class PolyglotSymbol:
    """Represents a language-agnostic code symbol definition."""

    name: str
    kind: SymbolKind
    signature: str
    line_start: int
    line_end: int
    docstring: str
    language: str
    parent_scope: str | None = None


@dataclass
class FileMetrics:
    """Quantified complexity and structural metrics for a source file."""

    cyclomatic_complexity: int = 1
    max_nesting_depth: int = 1
    lines_of_code: int = 0
    symbol_count: int = 0
    language: str = "unknown"


@dataclass
class PolyglotFileNode:
    """Complete structural model and AST representation of a parsed file."""

    file_path: str
    language: str
    symbols: list[PolyglotSymbol] = field(default_factory=list)
    metrics: FileMetrics = field(default_factory=FileMetrics)
    skipped: bool = False
    skip_reason: str = ""


class LanguageDetector:
    """Infers programming language identity from file path extensions."""

    @classmethod
    def detect(cls, file_path: Path | str) -> str | None:
        """Map path suffix to canonical language name or None if unsupported."""
        suffix = Path(file_path).suffix.lower()
        return EXT_TO_LANG.get(suffix)


class BoundaryGuard:
    """Enforces pre-flight file size and symlink directory containment."""

    @classmethod
    def verify(
        cls,
        target_path: Path,
        base_root: Path | None = None,
        max_size_bytes: int = MAX_FILE_SIZE_BYTES,
    ) -> tuple[bool, Path | None, str]:
        """Verify that target file exists, is bounded in size, and does not escape base_root."""
        ok_sym, resolved, err_sym = cls._verify_containment(target_path, base_root)
        if not ok_sym or resolved is None:
            return False, resolved, err_sym

        ok_size, err_size = cls._verify_size(resolved, max_size_bytes)
        if not ok_size:
            return False, resolved, err_size

        return True, resolved, ""

    @staticmethod
    def _check_root_containment(resolved: Path, base_root: Path) -> tuple[bool, str]:
        """Verify resolved path stays within workspace base root."""
        try:
            resolved_base = base_root.resolve()
            if not resolved.is_relative_to(resolved_base):
                return False, f"File points outside workspace root ({resolved})"
        except (OSError, RuntimeError) as err:
            return False, f"Workspace boundary verification error ({str(err)[:200]})"
        return True, ""

    @classmethod
    def _verify_containment(cls, target_path: Path, base_root: Path | None) -> tuple[bool, Path | None, str]:
        """Resolve target path symlinks and verify boundary containment."""
        try:
            resolved = target_path.resolve()
        except (OSError, RuntimeError) as err:
            return False, None, f"Symlink resolution failed ({str(err)[:200]})"

        if base_root is None:
            return True, resolved, ""

        ok, err = cls._check_root_containment(resolved, base_root)
        return ok, (resolved if ok else resolved), err

    @staticmethod
    def _verify_size(resolved: Path, max_size_bytes: int) -> tuple[bool, str]:
        try:
            st = resolved.stat()
            if st.st_size > max_size_bytes:
                return False, f"Size ({st.st_size} bytes) exceeds maximum limit ({max_size_bytes} bytes)"
        except (OSError, RuntimeError) as err:
            return False, f"File stat error ({str(err)[:200]})"
        return True, ""


class PolyglotComplexityCalculator:
    """Computes decision-point cyclomatic complexity and nesting depth."""

    @classmethod
    def calculate(cls, content: str, language: str) -> tuple[int, int]:
        """Calculate cyclomatic complexity M and max nesting depth for source content."""
        if language == "python":
            return cls._calculate_python(content)
        return cls._calculate_polyglot_tokens(content, language)

    @classmethod
    def _calculate_python(cls, content: str) -> tuple[int, int]:
        try:
            tree = ast.parse(content)
        except SyntaxError:
            return 1, 1

        complexity = 1 + sum(cls._node_decision_count(node) for node in ast.walk(tree))
        max_depth = cls._calculate_py_depth(tree)
        return complexity, max_depth

    @staticmethod
    def _node_decision_count(node: ast.AST) -> int:
        branch_types = (ast.If, ast.While, ast.For, ast.AsyncFor, ast.ExceptHandler, ast.Assert, ast.IfExp)
        if isinstance(node, branch_types):
            return 1
        if isinstance(node, ast.BoolOp):
            return len(node.values) - 1
        return 0

    @classmethod
    def _calculate_py_depth(cls, tree: ast.AST) -> int:
        def _get_depth(node: ast.AST, cur: int) -> int:
            nesting_types = (ast.If, ast.While, ast.For, ast.AsyncFor, ast.With, ast.AsyncWith, ast.Try)
            next_cur = cur + 1 if isinstance(node, nesting_types) else cur
            children = list(ast.iter_child_nodes(node))
            if not children:
                return next_cur
            return max(_get_depth(ch, next_cur) for ch in children)

        return _get_depth(tree, 0)

    @classmethod
    def _calculate_polyglot_tokens(cls, content: str, language: str) -> tuple[int, int]:
        complexity = 1 + cls._count_polyglot_decisions(content)
        depth = cls._calculate_brace_nesting(content)
        return complexity, depth

    @staticmethod
    def _count_polyglot_decisions(content: str) -> int:
        decision_patterns = [
            r"\bif\b",
            r"\belif\b",
            r"\bfor\b",
            r"\bwhile\b",
            r"\bcase\b",
            r"\bcatch\b",
            r"\bmatch\b",
            r"\bselect\b",
            r"&&",
            r"\|\|",
            r"\?",
        ]
        combined_re = re.compile("|".join(decision_patterns))
        return len(combined_re.findall(content))

    @staticmethod
    def _calculate_brace_nesting(content: str) -> int:
        """Calculate maximum brace nesting depth using flattened delta accumulator."""
        max_depth = 1
        current = 0
        for ch in content:
            current += (ch == "{") - (ch == "}")
            current = max(0, current)
            max_depth = max(max_depth, current)
        return max_depth


class PolyglotCSTParser:
    """Ingestion engine extracting symbols and calculating language-agnostic metrics."""

    def __init__(self, max_file_size_bytes: int = MAX_FILE_SIZE_BYTES) -> None:
        """Initialize CST parser with maximum allowable file size threshold."""
        self.max_file_size_bytes = max_file_size_bytes

    def parse_file(self, file_path: Path, base_root: Path | None = None) -> PolyglotFileNode:
        """Parse source file on disk applying pre-flight boundary guards."""
        lang = LanguageDetector.detect(file_path) or "unknown"
        ok, resolved_path, reason = BoundaryGuard.verify(
            file_path,
            base_root=base_root,
            max_size_bytes=self.max_file_size_bytes,
        )
        if not ok or resolved_path is None:
            return PolyglotFileNode(
                file_path=str(file_path),
                language=lang,
                skipped=True,
                skip_reason=reason,
            )

        try:
            content = resolved_path.read_text(encoding="utf-8", errors="replace")
        except OSError as err:
            return PolyglotFileNode(
                file_path=str(file_path),
                language=lang,
                skipped=True,
                skip_reason=f"Read error: {str(err)[:200]}",
            )

        return self.parse_content(content, str(file_path), lang)

    def parse_content(self, content: str, file_path: str, language: str) -> PolyglotFileNode:
        """Parse source content string and extract symbols and metrics."""
        symbols = self._extract_symbols(content, language)
        complexity, depth = PolyglotComplexityCalculator.calculate(content, language)
        loc = len([ln for ln in content.splitlines() if ln.strip() and not ln.strip().startswith(("#", "//"))])

        metrics = FileMetrics(
            cyclomatic_complexity=complexity,
            max_nesting_depth=depth,
            lines_of_code=loc,
            symbol_count=len(symbols),
            language=language,
        )

        return PolyglotFileNode(
            file_path=file_path,
            language=language,
            symbols=symbols,
            metrics=metrics,
        )

    def _extract_symbols(self, content: str, language: str) -> list[PolyglotSymbol]:
        if language == "python":
            return self._extract_python_symbols(content)
        if language == "rust":
            return self._extract_rust_symbols(content)
        if language == "go":
            return self._extract_go_symbols(content)
        if language in ("typescript", "javascript"):
            return self._extract_ts_symbols(content)
        if language == "bash":
            return self._extract_bash_symbols(content)
        return []

    @classmethod
    def _extract_python_symbols(cls, content: str) -> list[PolyglotSymbol]:
        try:
            tree = ast.parse(content)
        except SyntaxError:
            return []

        symbols: list[PolyglotSymbol] = []
        for node in tree.body:
            symbols.extend(cls._extract_py_node_symbols(node))
        return symbols

    @classmethod
    def _extract_py_node_symbols(cls, node: ast.AST) -> list[PolyglotSymbol]:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return [cls._build_py_fn_symbol(node)]
        if isinstance(node, ast.ClassDef):
            return cls._extract_py_class_members(node)
        return []

    @classmethod
    def _extract_py_class_members(cls, node: ast.ClassDef) -> list[PolyglotSymbol]:
        cls_sym = PolyglotSymbol(
            name=node.name,
            kind=SymbolKind.CLASS,
            signature=f"class {node.name}",
            line_start=node.lineno,
            line_end=getattr(node, "end_lineno", node.lineno),
            docstring=ast.get_docstring(node) or "",
            language="python",
        )
        symbols = [cls_sym]
        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                symbols.append(cls._build_py_fn_symbol(item, parent_scope=node.name, kind=SymbolKind.METHOD))
        return symbols

    @staticmethod
    def _build_py_fn_symbol(
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        parent_scope: str | None = None,
        kind: SymbolKind = SymbolKind.FUNCTION,
    ) -> PolyglotSymbol:
        return PolyglotSymbol(
            name=node.name,
            kind=kind,
            signature=f"def {node.name}(...)",
            line_start=node.lineno,
            line_end=getattr(node, "end_lineno", node.lineno),
            docstring=ast.get_docstring(node) or "",
            language="python",
            parent_scope=parent_scope,
        )

    @staticmethod
    def _extract_rust_symbols(content: str) -> list[PolyglotSymbol]:
        symbols: list[PolyglotSymbol] = []
        for match in RUST_STRUCT_RE.finditer(content):
            symbols.append(PolyglotSymbol(match.group(1), SymbolKind.STRUCT, f"struct {match.group(1)}", 1, 1, "", "rust"))
        for match in RUST_TRAIT_RE.finditer(content):
            symbols.append(PolyglotSymbol(match.group(1), SymbolKind.TRAIT, f"trait {match.group(1)}", 1, 1, "", "rust"))
        for match in RUST_FN_RE.finditer(content):
            symbols.append(PolyglotSymbol(match.group(1), SymbolKind.FUNCTION, f"fn {match.group(1)}", 1, 1, "", "rust"))
        return symbols

    @staticmethod
    def _extract_go_symbols(content: str) -> list[PolyglotSymbol]:
        symbols: list[PolyglotSymbol] = []
        for match in GO_TYPE_STRUCT_RE.finditer(content):
            symbols.append(PolyglotSymbol(match.group(1), SymbolKind.STRUCT, f"type {match.group(1)} struct", 1, 1, "", "go"))
        for match in GO_TYPE_INTERFACE_RE.finditer(content):
            symbols.append(PolyglotSymbol(match.group(1), SymbolKind.INTERFACE, f"type {match.group(1)} interface", 1, 1, "", "go"))
        for match in GO_FUNC_RE.finditer(content):
            symbols.append(PolyglotSymbol(match.group(1), SymbolKind.FUNCTION, f"func {match.group(1)}", 1, 1, "", "go"))
        return symbols

    @staticmethod
    def _extract_ts_symbols(content: str) -> list[PolyglotSymbol]:
        symbols: list[PolyglotSymbol] = []
        for match in TS_CLASS_RE.finditer(content):
            symbols.append(PolyglotSymbol(match.group(1), SymbolKind.CLASS, f"class {match.group(1)}", 1, 1, "", "typescript"))
        for match in TS_INTERFACE_RE.finditer(content):
            symbols.append(PolyglotSymbol(match.group(1), SymbolKind.INTERFACE, f"interface {match.group(1)}", 1, 1, "", "typescript"))
        for match in TS_FUNC_RE.finditer(content):
            symbols.append(PolyglotSymbol(match.group(1), SymbolKind.FUNCTION, f"function {match.group(1)}", 1, 1, "", "typescript"))
        return symbols

    @staticmethod
    def _extract_bash_symbols(content: str) -> list[PolyglotSymbol]:
        symbols: list[PolyglotSymbol] = []
        for match in BASH_FUNC_RE.finditer(content):
            symbols.append(PolyglotSymbol(match.group(1), SymbolKind.FUNCTION, f"{match.group(1)}()", 1, 1, "", "bash"))
        return symbols


def run_demo_matrix() -> list[tuple[str, str, int, int]]:
    """Run demonstration parsing across Python, Rust, Go, TypeScript, and Bash."""
    parser = PolyglotCSTParser()
    samples = [
        ("Python", "def process_data(x):\n    if x > 0: return x * 2\n    return 0", "python"),
        ("Rust", "pub struct Queue;\npub fn push(val: u32) { if val > 0 {} }", "rust"),
        ("Go", "type Store struct{}\nfunc (s Store) Get() string { return \"ok\" }", "go"),
        ("TypeScript", "interface Event { id: string }\nfunction handle(e: Event) { return e.id }", "typescript"),
        ("Bash", "backup_db() { if [ -f /tmp/db ]; then cp /tmp/db /backup; fi }", "bash"),
    ]

    results: list[tuple[str, str, int, int]] = []
    for lang_name, code, lang_id in samples:
        node = parser.parse_content(code, f"sample.{lang_id}", lang_id)
        results.append((lang_name, lang_id, len(node.symbols), node.metrics.cyclomatic_complexity))
    return results


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint for polyglot source scanning and demonstration."""
    arg_parser = argparse.ArgumentParser(description="Polyglot CST Ingestion Engine")
    arg_parser.add_argument("--demo", action="store_true", help="Execute multi-language ingestion demonstration")
    arg_parser.add_argument("--scan", type=str, help="Scan a file path and print extracted symbols and metrics")
    args = arg_parser.parse_args(argv)

    if args.scan:
        path = Path(args.scan)
        parser = PolyglotCSTParser()
        node = parser.parse_file(path)
        _print_file_node(node)
        return 0

    _print_scorecard(run_demo_matrix())
    return 0


def _print_file_node(node: PolyglotFileNode) -> None:
    print(f"File: {node.file_path} (Language: {node.language})")
    if node.skipped:
        print(f"  [SKIPPED] {node.skip_reason}")
        return
    print(f"  LOC: {node.metrics.lines_of_code} | Complexity (M): {node.metrics.cyclomatic_complexity} | Depth: {node.metrics.max_nesting_depth}")
    print(f"  Symbols Extracted ({len(node.symbols)}):")
    for s in node.symbols:
        print(f"    - [{s.kind.value}] {s.name:<24} {s.signature}")


def _print_scorecard(matrix: list[tuple[str, str, int, int]]) -> None:
    print("==========================================================================")
    print("🌐 POLYGLOT CST INGESTION SCORECARD")
    print("--------------------------------------------------------------------------")
    print(f"{'LANGUAGE':<14} | {'CANONICAL ID':<14} | {'SYMBOLS':<10} | {'COMPLEXITY (M)':<14}")
    print("--------------------------------------------------------------------------")
    for lang, lang_id, count, comp in matrix:
        print(f"{lang:<14} | {lang_id:<14} | {count:<10} | {comp:<14}")
    print("==========================================================================")


if __name__ == "__main__":
    sys.exit(main())
