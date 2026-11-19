#!/usr/bin/env python3
"""High-Performance AST Context Packer with Binary Search Truncation.

Packs the top-level imports, functions and classes of a Python source file into an
estimated prompt context token budget without producing truncated function/class blocks.
Uses binary search boundary convergence over structured symbol units.
"""

from __future__ import annotations

import ast
import copy
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

# The common rule of thumb is about four characters of English text per token. For ASCII,
# characters and UTF-8 bytes are the same. Applied to `len(text)`, the rule under-counted
# non-ASCII source, where one character takes up to four UTF-8 bytes, so this estimate
# divides the UTF-8 byte count by four. It is a heuristic, not a tokenizer count.
DEFAULT_TOKEN_CHAR_RATIO: Final[int] = 4


class ContextDetailLevel(StrEnum):
    """Fidelity level for packed symbol representation."""

    FULL = "FULL"
    DOCSTRINGS = "DOCSTRINGS"
    SIGNATURES = "SIGNATURES"


@dataclass(frozen=True)
class AstSymbol:
    """Extracted top-level structural symbol from Python AST."""

    name: str
    kind: str
    signature: str
    docstring: str
    body_summary: str
    full_source: str
    line_number: int

    def render(self, level: ContextDetailLevel) -> str:
        """Render symbol text according to requested fidelity level."""
        if self.kind == "import" or level == ContextDetailLevel.FULL:
            return self.full_source
        if level == ContextDetailLevel.DOCSTRINGS and self.docstring:
            # `repr` rather than interpolation between two `"""` delimiters. A docstring
            # containing a quote, a backslash or its own triple quote closed the literal
            # early and emitted Python that will not parse — and this tier exists to hand
            # a model something it can read as code.
            return f"{self.signature}\n    {self.docstring!r}\n    ..."
        return f"{self.signature}\n    ..."


def estimate_tokens(text: str) -> int:
    """Fast character-based heuristic token estimator."""
    if not text:
        return 0
    measured = len(text.encode("utf-8"))
    return max(1, (measured + DEFAULT_TOKEN_CHAR_RATIO - 1) // DEFAULT_TOKEN_CHAR_RATIO)


def _statement_source(source_code: str, node: ast.stmt) -> str:
    """Return the exact source of one top-level statement, from its start to its end.

    `ast.get_source_segment` splits lines the way the tokenizer does. `str.splitlines()`
    also breaks on a form feed, which is legal in Python source and is a page break by
    convention, so one form feed shifted every later slice by one line. A whole-line slice
    also took a second statement that shares the line (`import os; x = (` did not parse).
    """
    segment = ast.get_source_segment(source_code, node) or ""
    return segment.replace("\r\n", "\n").replace("\r", "\n")


def _extract_docstring(node: ast.AST) -> str:
    """Extract docstring text if present on AST node."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return ast.get_docstring(node) or ""
    return ""


def _declaration_header(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> str:
    """Render the header of a def or class from the AST, so that it is always complete.

    Source slicing failed three ways: `def f(): return 1` gave a bare `:`, a cut at the
    first colon turned `def f(x: int): return x` into `def f(x:`, and a multi-line header
    whose body shared its last line lost that line (`def g(:`). `ast.unparse` writes the
    header from the parsed node and needs no slicing. Decorators stay out, as in FULL.
    """
    stub = copy.copy(node)
    stub.decorator_list = []
    stub.body = [ast.Expr(ast.Constant(...))]
    return ast.unparse(stub).rsplit("\n", 1)[0]


def extract_ast_symbols(source_code: str) -> list[AstSymbol]:
    """Parse Python source code and extract structured top-level AST symbols."""
    try:
        tree = ast.parse(source_code)
    except SyntaxError:
        return []

    symbols: list[AstSymbol] = []

    for node in tree.body:
        sym = _parse_top_level_node(node, source_code)
        if sym is not None:
            symbols.append(sym)

    return symbols


def _parse_top_level_node(node: ast.stmt, source_code: str) -> AstSymbol | None:
    """Transform an individual AST node into a structured AstSymbol."""
    full_source = _statement_source(source_code, node)
    docstring = _extract_docstring(node)

    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        sig = _declaration_header(node)
        return AstSymbol(node.name, "function", sig, docstring, "...", full_source, node.lineno)
    if isinstance(node, ast.ClassDef):
        sig = _declaration_header(node)
        return AstSymbol(node.name, "class", sig, docstring, "...", full_source, node.lineno)
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return AstSymbol("import", "import", full_source, "", "", full_source, node.lineno)

    return None


def render_symbol_slice(symbols: Sequence[AstSymbol], count: int, level: ContextDetailLevel) -> str:
    """Render the first `count` symbols separated by clean spacing."""
    rendered_blocks = [s.render(level) for s in symbols[:count]]
    return "\n\n".join(rendered_blocks)


def binary_search_pack_symbols(
    symbols: Sequence[AstSymbol],
    budget_tokens: int,
    level: ContextDetailLevel,
) -> tuple[str, int]:
    """Use monotonic binary search to find maximum symbol count fitting within token budget.

    Returns tuple of (rendered_context, symbol_count).
    """
    if not symbols or budget_tokens <= 0:
        return "", 0

    low = 0
    high = len(symbols)
    best_count = 0
    best_rendered = ""

    while low <= high:
        mid = (low + high) // 2
        if mid == 0:
            low = mid + 1
            continue

        candidate = render_symbol_slice(symbols, mid, level)
        tokens = estimate_tokens(candidate)

        if tokens <= budget_tokens:
            best_count = mid
            best_rendered = candidate
            low = mid + 1
        else:
            high = mid - 1

    return best_rendered, best_count


def pack_source_to_budget(
    source_code: str,
    budget_tokens: int,
) -> tuple[str, ContextDetailLevel, int]:
    """Pack source code to fit token budget, dynamically degrading detail levels if needed.

    Levels evaluated: FULL -> DOCSTRINGS -> SIGNATURES.
    Each symbol renders as a complete top-level statement, so the output parses as a
    Python module: FULL keeps each statement's exact source, and the other tiers render
    the header from the AST and add the body `...`.
    """
    symbols = extract_ast_symbols(source_code)
    if not symbols or budget_tokens <= 0:
        return "", ContextDetailLevel.SIGNATURES, 0

    for level in (ContextDetailLevel.FULL, ContextDetailLevel.DOCSTRINGS, ContextDetailLevel.SIGNATURES):
        rendered, count = binary_search_pack_symbols(symbols, budget_tokens, level)
        if count == len(symbols):
            return rendered, level, count

    # Fallback to maximal partial packing at SIGNATURES level
    rendered, count = binary_search_pack_symbols(symbols, budget_tokens, ContextDetailLevel.SIGNATURES)
    return rendered, ContextDetailLevel.SIGNATURES, count
