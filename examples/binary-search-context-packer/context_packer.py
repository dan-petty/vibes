#!/usr/bin/env python3
"""High-Performance AST Context Packer with Binary Search Truncation.

Packs multi-symbol Python source files into an exact prompt context token budget
without breaking AST syntactic validity or producing truncated function/class blocks.
Uses binary search boundary convergence over structured symbol units.
"""

from __future__ import annotations

import ast
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

# tiktoken's rule of thumb is roughly four *bytes* per token, not four code points. Applied
# to `len(text)` it under-counted every non-ASCII source by up to four times: a Japanese
# docstring measured 19 tokens against a real 76, so the packer reported a budget met and
# handed over four times what was asked for.
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


def _source_lines(source_code: str) -> list[str]:
    """Split source the way the Python tokenizer does, so `ast` line numbers index correctly.

    `str.splitlines()` also breaks on form feed, vertical tab, file/group/record separators,
    NEL and the Unicode line separators. The tokenizer does not: it recognises `\\n`,
    `\\r\\n` and `\\r` only. A single form feed — legal, and conventional as a page break in
    Python source — therefore shifted every index after it by one, and the packer emitted
    fragments sliced one line off from where `ast` said they were.
    """
    return source_code.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def _extract_docstring(node: ast.AST) -> str:
    """Extract docstring text if present on AST node."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return ast.get_docstring(node) or ""
    return ""


def _extract_declaration_signature(
    node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef, lines: Sequence[str]
) -> str:
    """Extract the declaration lines of a def or class, up to the start of its body.

    Functions and classes differ in what they declare and not at all in how the
    declaration is sliced, which is why the two extractors were byte-identical.
    """
    start_line = node.lineno - 1
    body_start = node.body[0].lineno - 1 if node.body else start_line + 1
    # `def f(): return 1` puts the body on the declaration line, so `body[0].lineno` equals
    # `node.lineno` and the slice was empty — the symbol rendered as a bare `:` and the whole
    # packed context stopped being parsable Python. Always take the declaration line itself.
    body_start = max(body_start, start_line + 1)
    sig_lines = [lines[i] for i in range(start_line, min(len(lines), body_start))]
    sig = "\n".join(sig_lines).rstrip()
    if node.body and node.body[0].lineno == node.lineno:
        # Inline suite: keep the header, drop the statement that followed the colon.
        head, _, _ = sig.partition(":")
        sig = f"{head}:"
    return sig if sig.endswith(":") else f"{sig}:"


def extract_ast_symbols(source_code: str) -> list[AstSymbol]:
    """Parse Python source code and extract structured top-level AST symbols."""
    try:
        tree = ast.parse(source_code)
    except SyntaxError:
        return []

    lines = _source_lines(source_code)
    symbols: list[AstSymbol] = []

    for node in tree.body:
        sym = _parse_top_level_node(node, lines)
        if sym is not None:
            symbols.append(sym)

    return symbols


def _parse_top_level_node(node: ast.AST, lines: Sequence[str]) -> AstSymbol | None:
    """Transform an individual AST node into a structured AstSymbol."""
    start = node.lineno - 1
    end = getattr(node, "end_lineno", node.lineno)
    full_source = "\n".join(lines[start:end])
    docstring = _extract_docstring(node)

    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        sig = _extract_declaration_signature(node, lines)
        return AstSymbol(node.name, "function", sig, docstring, "...", full_source, node.lineno)
    if isinstance(node, ast.ClassDef):
        sig = _extract_declaration_signature(node, lines)
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
    Guarantees that the resulting output forms a valid Python module syntax tree.
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
