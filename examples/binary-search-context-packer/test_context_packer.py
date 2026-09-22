"""Comprehensive test suite for High-Performance AST Context Packer."""

from __future__ import annotations

import ast

from context_packer import (
    ContextDetailLevel,
    binary_search_pack_symbols,
    estimate_tokens,
    extract_ast_symbols,
    pack_source_to_budget,
)

SAMPLE_MODULE = """
import os
import sys

def calculate_hash(data: str) -> str:
    \"\"\"Calculate sha256 checksum.\"\"\"
    import hashlib
    return hashlib.sha256(data.encode()).hexdigest()

class DataVault:
    \"\"\"Thread-safe in-memory key-value cache.\"\"\"
    def __init__(self) -> None:
        self._store = {}

    def get(self, key: str) -> str | None:
        \"\"\"Retrieve key from vault.\"\"\"
        return self._store.get(key)

def verify_token(token: str) -> bool:
    \"\"\"Validate JWT payload signature.\"\"\"
    return len(token) > 10
"""


def test_extract_ast_symbols() -> None:
    """Ensure top-level functions, classes, and imports are cleanly extracted."""
    symbols = extract_ast_symbols(SAMPLE_MODULE)
    names = [s.name for s in symbols]
    assert (
        len(symbols),
        names,
        symbols[0].kind,
        symbols[2].name,
    ) == (5, ["import", "import", "calculate_hash", "DataVault", "verify_token"], "import", "calculate_hash")


def test_estimate_tokens() -> None:
    """Verify monotonic character-based token estimation."""
    assert (
        estimate_tokens(""),
        estimate_tokens("a"),
        estimate_tokens("1234"),
        estimate_tokens("12345"),
    ) == (0, 1, 1, 2)


def test_pack_source_large_budget_returns_full() -> None:
    """Ensure abundant token budget returns full fidelity source code."""
    rendered, level, count = pack_source_to_budget(SAMPLE_MODULE, budget_tokens=5000)
    parsed = ast.parse(rendered)
    assert (
        level,
        count,
        len(parsed.body) >= 5,
        "calculate_hash" in rendered,
        "DataVault" in rendered,
    ) == (ContextDetailLevel.FULL, 5, True, True, True)


def test_pack_source_constrained_budget_degrades_to_signatures() -> None:
    """Ensure tight token budget degrades to signatures without breaking AST syntax."""
    rendered, level, _count = pack_source_to_budget(SAMPLE_MODULE, budget_tokens=50)
    # Output must be syntactically valid Python
    parsed = ast.parse(rendered)
    tokens = estimate_tokens(rendered)
    assert (
        tokens <= 50,
        level in (ContextDetailLevel.DOCSTRINGS, ContextDetailLevel.SIGNATURES),
        len(parsed.body) > 0,
    ) == (True, True, True)


def test_binary_search_monotonicity() -> None:
    """Verify binary search packs maximal symbol count within budget."""
    symbols = extract_ast_symbols(SAMPLE_MODULE)
    # Budget that fits exactly 2 symbols
    candidate_2 = "\n\n".join([s.render(ContextDetailLevel.FULL) for s in symbols[:2]])
    tokens_2 = estimate_tokens(candidate_2)

    rendered, count = binary_search_pack_symbols(symbols, budget_tokens=tokens_2, level=ContextDetailLevel.FULL)
    assert (count, estimate_tokens(rendered) <= tokens_2) == (2, True)


def test_pack_source_empty_and_zero_budget() -> None:
    """Ensure edge cases for empty string and zero budget return empty results cleanly."""
    r1, _l1, c1 = pack_source_to_budget("", 100)
    r2, _l2, c2 = pack_source_to_budget(SAMPLE_MODULE, 0)
    assert (r1, c1, r2, c2) == ("", 0, "", 0)


def test_extract_symbols_syntax_error() -> None:
    """Ensure invalid syntax returns empty list gracefully without raising exception."""
    symbols = extract_ast_symbols("def broken_syntax(")
    assert (len(symbols), symbols) == (0, [])
